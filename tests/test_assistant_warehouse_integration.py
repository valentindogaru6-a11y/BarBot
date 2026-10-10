from pathlib import Path

import pytest

from app.models.assistant import AssistantIntent
from app.models.catalog import Catalog
from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.assistant import AssistantToolRegistry
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.seed_service import build_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_seed_database import WarehouseSeedDatabase

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def warehouse(tmp_path):
    database_path = tmp_path / "assistant-warehouse.db"
    user_database = UserDatabase(database_path)
    user_database.initialize()
    WarehouseSeedDatabase(database_path).import_seed(
        build_warehouse_seed(
            ROOT / "source" / "warehouse_product_candidates.json"
        ),
        apply=True,
    )
    users = UserService(user_database)
    users.ensure_user(20)
    users.set_role(20, Role.BARTENDER, actor_user_id=1)
    repository = WarehouseOperationsDatabase(database_path)
    service = WarehouseOperationsService(
        repository,
        AuthService(frozenset({1}), users),
    )
    return users, repository, service


def _ai_writeoff() -> str:
    action = AssistantToolRegistry(Catalog(version=1)).execute(
        AssistantIntent(
            intent="prepare_writeoff",
            confidence=9_000,
            items=[
                {
                    "product_text": "апельсин",
                    "quantity_text": "0,15",
                    "unit_text": "кг",
                    "comment_text": "испорчен",
                }
            ],
        )
    )
    assert action.kind == "prepare_writeoff"
    assert action.raw_intake is not None
    return action.raw_intake


def test_ai_intent_creates_idempotent_draft_then_confirmation_creates_operation(
    warehouse,
) -> None:
    _, repository, service = warehouse
    raw_intake = _ai_writeoff()
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=700,
        source_message_id=800,
        raw_text=raw_intake,
    )
    repeated = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=700,
        source_message_id=800,
        raw_text=raw_intake,
    )
    assert batch["id"] == repeated["id"]
    assert repository.list_operations(batch["shift_id"]) == []

    operations = service.commit_batch(batch["id"], actor_user_id=20)
    assert len(operations) == 1
    assert len(repository.list_operations(batch["shift_id"])) == 1


def test_ai_draft_cancel_creates_no_operation(warehouse) -> None:
    _, repository, service = warehouse
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=701,
        source_message_id=801,
        raw_text=_ai_writeoff(),
    )
    service.cancel_batch(batch["id"], actor_user_id=20)
    assert repository.list_operations(batch["shift_id"]) == []


def test_ai_draft_role_change_before_confirmation_blocks_operation(
    warehouse,
) -> None:
    users, repository, service = warehouse
    batch = service.prepare_batch(
        actor_user_id=20,
        source_chat_id=702,
        source_message_id=802,
        raw_text=_ai_writeoff(),
    )
    users.set_role(20, Role.USER, actor_user_id=1)
    with pytest.raises(AccessDeniedError):
        service.commit_batch(batch["id"], actor_user_id=20)
    assert repository.list_operations(batch["shift_id"]) == []
