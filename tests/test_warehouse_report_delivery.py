"""Phase-7 recipient, verification, and delivery behavior on temporary data."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.recipient_service import (
    WarehouseRecipientService,
    parse_numeric_chat_id,
)
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.seed_service import load_warehouse_seed
from app.services.warehouse.telegram_delivery import (
    TelegramChatVerificationError,
    TelegramChatVerifier,
)
from app.services.warehouse.xlsx_report_service import WarehouseXlsxReportService
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_delivery_database import (
    WarehouseDeliveryDatabase,
    WarehouseRecipientConflict,
)
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class FakeSender:
    def __init__(
        self,
        database_path: Path,
        *,
        failing: set[int] | None = None,
        pause: float = 0,
    ) -> None:
        self.database_path = database_path
        self.failing = failing or set()
        self.pause = pause
        self.calls: list[tuple[int, Path, str]] = []
        self.database_was_unlocked: list[bool] = []

    async def send_report(self, *, chat_id, path, filename, caption) -> int:
        connection = sqlite3.connect(self.database_path, timeout=0.1)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            self.database_was_unlocked.append(True)
        finally:
            connection.close()
        self.calls.append((chat_id, Path(path), filename))
        if self.pause:
            await asyncio.sleep(self.pause)
        if chat_id in self.failing:
            raise RuntimeError("secret-token-must-not-be-stored")
        return 1000 + len(self.calls)


@pytest.fixture(scope="module")
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse-delivery") / "template.db"
    UserDatabase(path).initialize()
    seed = load_warehouse_seed(ROOT / "source" / "warehouse_products_seed.json")
    WarehouseSeedDatabase(path).import_seed(seed, apply=True)
    return path


@pytest.fixture
def environment(database_template: Path, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    path = project / "warehouse.db"
    shutil.copyfile(database_template, path)
    users = UserService(UserDatabase(path))
    for user_id in (10, 20, 30, 40):
        users.ensure_user(user_id)
    users.set_role(10, Role.ADMIN, actor_user_id=1)
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    users.set_role(30, Role.ACCOUNTANT, actor_user_id=10)
    access = AuthService(frozenset({1}), users)
    clock = FrozenClock()
    operations = WarehouseOperationsService(
        WarehouseOperationsDatabase(path), access, clock=clock
    )
    closing = WarehouseClosingService(
        WarehouseClosingDatabase(path), operations, access, clock=clock
    )
    reports = WarehouseReportService(
        WarehouseReportsDatabase(path),
        operations,
        access,
        project / "reports" / "warehouse",
        clock=clock,
        xlsx_service=WarehouseXlsxReportService(
            project / "reports" / "warehouse", project_root=project
        ),
    )
    repository = WarehouseDeliveryDatabase(path)
    recipients = WarehouseRecipientService(repository, access, clock=clock)
    return path, users, access, closing, reports, repository, recipients, clock


def _ready_report(closing, reports, actor: int = 20):
    session = closing.start(actor_user_id=actor)
    for requirement in session["requirements"]:
        if requirement["status"] == "pending":
            session = closing.mark_not_applicable(
                int(session["id"]), int(requirement["id"]),
                "Остатков не было", actor_user_id=actor,
            )
    session = closing.resume(int(session["id"]), actor_user_id=actor)
    closing.finalize(
        int(session["id"]), expected_revision=int(session["revision"]),
        actor_user_id=actor,
    )
    return reports.build_final(
        int(session["shift_id"]), actor_user_id=actor, allow_empty=True
    )


def _add(recipients, chat_id: int, title: str = "Бухгалтерия"):
    return recipients.create_verified(
        chat_id=chat_id,
        title=title,
        chat_type="supergroup" if chat_id < 0 else "private",
        actor_user_id=10,
    )


def test_numeric_chat_id_parser_rejects_username_and_zero() -> None:
    assert parse_numeric_chat_id(" -100123 ") == -100123
    assert parse_numeric_chat_id("42") == 42
    for value in ("@accounting", "group", "0", "+42", "12.3"):
        with pytest.raises(ValueError, match="числовой chat ID"):
            parse_numeric_chat_id(value)


def test_recipient_crud_permissions_revision_and_audit(environment) -> None:
    path, _, _, _, _, _, recipients, _ = environment
    created = _add(recipients, -1001, "  Бухгалтерия  ")
    assert created["title"] == "Бухгалтерия" and created["revision"] == 1
    with pytest.raises(WarehouseRecipientConflict):
        _add(recipients, -1001)
    renamed = recipients.rename(
        int(created["id"]), "Финансы", expected_revision=1, actor_user_id=10
    )
    with pytest.raises(WarehouseRecipientConflict):
        recipients.rename(
            int(created["id"]), "Устарело", expected_revision=1,
            actor_user_id=10,
        )
    inactive = recipients.set_active(
        int(created["id"]), is_active=False,
        expected_revision=int(renamed["revision"]), actor_user_id=10,
    )
    active = recipients.set_active(
        int(created["id"]), is_active=True,
        expected_revision=int(inactive["revision"]), actor_user_id=10,
    )
    assert active["revision"] == 4 and recipients.active_count(actor_user_id=20) == 1
    for actor in (20, 30, 40):
        with pytest.raises(AccessDeniedError):
            recipients.list_recipients(actor_user_id=actor)
    with sqlite3.connect(path) as connection:
        events = {
            row[0] for row in connection.execute(
                "SELECT event_type FROM warehouse_audit_events "
                "WHERE entity_type='warehouse_report_recipient'"
            )
        }
    assert events == {
        "warehouse_report_recipient_created",
        "warehouse_report_recipient_updated",
        "warehouse_report_recipient_deactivated",
        "warehouse_report_recipient_activated",
    }


def test_chat_verifier_uses_get_chat_and_never_test_sends() -> None:
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=-1005, type="supergroup", title="Finance"
        )),
        get_me=AsyncMock(return_value=SimpleNamespace(id=77)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_document=AsyncMock(),
    )
    result = asyncio.run(TelegramChatVerifier(bot).verify(-1005))
    assert (result.chat_id, result.title, result.chat_type) == (
        -1005, "Finance", "supergroup"
    )
    bot.get_chat.assert_awaited_once_with(-1005)
    bot.get_chat_member.assert_awaited_once_with(-1005, 77)
    bot.send_document.assert_not_called()


def test_chat_verifier_rejects_unreachable_and_unwritable_chat() -> None:
    unreachable = SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("bad")))
    with pytest.raises(TelegramChatVerificationError, match="Добавьте бота"):
        asyncio.run(TelegramChatVerifier(unreachable).verify(-1))
    channel_bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(
            type="channel", title="Finance"
        )),
        get_me=AsyncMock(return_value=SimpleNamespace(id=77)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
    )
    with pytest.raises(TelegramChatVerificationError, match="права публикации"):
        asyncio.run(TelegramChatVerifier(channel_bot).verify(-2))


def test_zero_recipients_does_not_claim_success(environment) -> None:
    path, _, access, closing, reports, repository, _, clock = environment
    ready = _ready_report(closing, reports)
    sender = FakeSender(path)
    delivery = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    result = asyncio.run(delivery.deliver(
        int(ready["report"]["id"]), actor_user_id=20
    ))
    assert result["deliveries"] == []
    assert result["delivered"] == result["failed"] == 0
    assert sender.calls == []


def test_manual_send_uses_existing_ready_xlsx_after_recipient_is_added(environment) -> None:
    path, _, access, closing, reports, repository, recipients, clock = environment
    ready = _ready_report(closing, reports)
    report_id = int(ready["report"]["id"])
    sender = FakeSender(path)
    delivery = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    assert asyncio.run(delivery.deliver(report_id, actor_user_id=20))["deliveries"] == []
    _add(recipients, -1501)
    result = asyncio.run(delivery.deliver(report_id, actor_user_id=20))
    assert result["delivered"] == 1
    assert sender.calls == [
        (-1501, ready["artifact"].path, ready["artifact"].path.name)
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_reports WHERE shift_id=?",
            (ready["report"]["shift_id"],),
        ).fetchone()[0] == 1


def test_multiple_recipients_partial_failure_retry_and_idempotency(environment) -> None:
    path, _, access, closing, reports, repository, recipients, clock = environment
    _add(recipients, -2001, "One")
    _add(recipients, -2002, "Two")
    ready = _ready_report(closing, reports)
    report_id = int(ready["report"]["id"])
    sender = FakeSender(path, failing={-2002})
    delivery = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    first = asyncio.run(delivery.deliver(report_id, actor_user_id=20))
    assert (first["delivered"], first["failed"]) == (1, 1)
    assert all(sender.database_was_unlocked)
    assert len({call[1] for call in sender.calls}) == 1
    assert sender.calls[0][1] == ready["artifact"].path
    assert reports.report_card(report_id, actor_user_id=30)["lines"] == ready["report"]["lines"]

    sender.failing.clear()
    second = asyncio.run(delivery.deliver(
        report_id, actor_user_id=20, retry_failed=True
    ))
    assert (second["delivered"], second["failed"]) == (2, 0)
    assert [call[0] for call in sender.calls].count(-2001) == 1
    assert [call[0] for call in sender.calls].count(-2002) == 2
    before = len(sender.calls)
    repeated = asyncio.run(delivery.deliver(report_id, actor_user_id=20))
    assert repeated["delivered"] == 2 and len(sender.calls) == before
    assert sorted(row["attempt_count"] for row in second["deliveries"]) == [1, 2]
    with sqlite3.connect(path) as connection:
        stored_error = connection.execute(
            "SELECT last_error_text FROM warehouse_report_deliveries "
            "WHERE recipient_id=(SELECT id FROM warehouse_report_recipients "
            "WHERE chat_id=-2002)"
        ).fetchone()[0]
        assert stored_error is None
        events = {
            row[0] for row in connection.execute(
                "SELECT event_type FROM warehouse_audit_events "
                "WHERE entity_type IN ('warehouse_report_delivery', 'warehouse_report')"
            )
        }
    assert {
        "warehouse_report_delivery_claimed",
        "warehouse_report_delivery_succeeded",
        "warehouse_report_delivery_failed",
        "warehouse_report_delivery_retry_requested",
    } <= events


def test_failed_error_is_sanitized_before_retry(environment) -> None:
    path, _, access, closing, reports, repository, recipients, clock = environment
    _add(recipients, -3001)
    ready = _ready_report(closing, reports)
    sender = FakeSender(path, failing={-3001})
    service = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    asyncio.run(service.deliver(int(ready["report"]["id"]), actor_user_id=20))
    with sqlite3.connect(path) as connection:
        code, text = connection.execute(
            "SELECT last_error_code, last_error_text "
            "FROM warehouse_report_deliveries"
        ).fetchone()
    assert code == "runtimeerror"
    assert text == "Telegram не принял документ"
    assert "secret" not in text.casefold()


def test_duplicate_concurrent_delivery_sends_once(environment) -> None:
    path, _, access, closing, reports, repository, recipients, clock = environment
    _add(recipients, -4001)
    ready = _ready_report(closing, reports)
    sender = FakeSender(path, pause=0.05)
    service = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )

    async def run_both():
        return await asyncio.gather(
            service.deliver(int(ready["report"]["id"]), actor_user_id=20),
            service.deliver(int(ready["report"]["id"]), actor_user_id=20),
        )

    asyncio.run(run_both())
    assert len(sender.calls) == 1
    assert repository.deliveries_for_report(int(ready["report"]["id"]))[0]["status"] == "delivered"


def test_stale_sending_is_recovered_only_by_explicit_retry(environment) -> None:
    path, _, access, closing, reports, repository, recipients, clock = environment
    _add(recipients, -5001)
    ready = _ready_report(closing, reports)
    report_id = int(ready["report"]["id"])
    rows = repository.prepare_deliveries(
        report_id, actor_user_id=20, now="2026-09-10T18:00:00Z",
        stale_before="2026-09-10T17:45:00Z", retry_failed=False,
        authorize=lambda: access.require_warehouse_report_delivery(20),
    )
    repository.claim_delivery(
        int(rows[0]["id"]), allow_failed=False, actor_user_id=20,
        now="2026-09-10T18:00:00Z",
        authorize=lambda: access.require_warehouse_report_delivery(20),
    )
    sender = FakeSender(path)
    service = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    normal = asyncio.run(service.deliver(report_id, actor_user_id=20))
    assert normal["pending"] == 1 and sender.calls == []
    retried = asyncio.run(service.deliver(
        report_id, actor_user_id=20, retry_failed=True
    ))
    assert retried["delivered"] == 1 and len(sender.calls) == 1
    assert retried["deliveries"][0]["attempt_count"] == 2


def test_role_is_rechecked_at_claim_and_history_matrix(environment, monkeypatch) -> None:
    path, users, access, closing, reports, repository, recipients, clock = environment
    _add(recipients, -6001)
    ready = _ready_report(closing, reports)
    report_id = int(ready["report"]["id"])
    sender = FakeSender(path)
    service = WarehouseReportDeliveryService(
        repository, reports, access, sender, clock=clock
    )
    original = repository.prepare_deliveries

    def prepare_then_revoke(*args, **kwargs):
        rows = original(*args, **kwargs)
        users.set_role(20, Role.USER, actor_user_id=10)
        return rows

    monkeypatch.setattr(repository, "prepare_deliveries", prepare_then_revoke)
    with pytest.raises(AccessDeniedError):
        asyncio.run(service.deliver(report_id, actor_user_id=20))
    assert sender.calls == []
    users.set_role(20, Role.BARTENDER, actor_user_id=10)
    monkeypatch.setattr(repository, "prepare_deliveries", original)
    asyncio.run(service.deliver(report_id, actor_user_id=20))
    assert service.history(report_id, actor_user_id=30)["can_view_details"]
    assert service.history(report_id, actor_user_id=10)["can_view_details"]
    bartender = service.history(report_id, actor_user_id=20)
    assert not bartender["can_view_details"] and bartender["details"] == []
    with pytest.raises(AccessDeniedError):
        service.history(report_id, actor_user_id=40)
