import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import assistant
from app.models.user import Role
from app.config import Settings
from app.services.auth_service import AuthService
from app.services.assistant.capabilities import CapabilityBinding, CapabilityRegistry
from app.services.assistant.capability_controller import AssistantCapabilityController
from app.services.assistant.proposals import AssistantActionProposalService
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.services.access_service import AccessDeniedError
from app.services.user_service import UserService
from app.storage.assistant_action_database import AssistantActionDatabase
from app.storage.user_database import UserDatabase


class Attendance:
    def status(self, _actor):
        return None

    def open_sessions(self, _actor):
        return []


class Reports:
    def history(self, *, actor_user_id, page):
        del actor_user_id, page
        return [], 0


def _controller(path: Path):
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    for user_id, name, role in (
        (10, "Admin", Role.ADMIN),
        (20, "Anna", Role.BARTENDER),
        (21, "Boris", Role.BARTENDER),
        (30, "Accountant", Role.ACCOUNTANT),
    ):
        users.ensure_user(user_id, first_name=name)
        users.set_role(user_id, role, actor_user_id=10)
    access = AuthService(frozenset(), users)
    registry = CapabilityRegistry()
    settings = Settings(
        _env_file=None, bot_token="test", ai_assistant_enabled=True,
        ai_access_mode="role_based", warehouse_enabled=True,
    )
    policy = AssistantAccessPolicy(access, registry, settings)
    proposals = AssistantActionProposalService(
        AssistantActionDatabase(path), access, registry, policy
    )
    return AssistantCapabilityController(
        registry,
        proposals,
        access,
        policy,
        flags={"ai_assistant_enabled": True, "warehouse_enabled": True},
        users=users,
        attendance=Attendance(),
        reports=Reports(),
    )


@pytest.mark.parametrize(
    "phrase",
    (
        "что ты умеешь делать?",
        "Что ты теперь умеешь делать?",
        "что ты умеешь?",
        "что ты можешь делать?",
        "какие у тебя возможности?",
        "какие функции тебе доступны?",
        "какие команды тебе доступны?",
        "что ті умеешь делать?",
        "расскажи, что ты вообще умеешь",
    ),
)
def test_capability_questions_are_local_and_do_not_call_provider(
    tmp_path: Path,
    phrase: str,
) -> None:
    controller = _controller(tmp_path / "controller.db")
    result = controller.dispatch(
        phrase,
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
    )
    assert result is not None
    assert result.capability_id == "capabilities.list"
    assert "Открыть мою смену" in result.message

    provider = SimpleNamespace(interpret=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        chat=SimpleNamespace(id=10, type="private"),
        message_id=1,
        answer=AsyncMock(),
    )
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=phrase,
            catalog_path=tmp_path / "unused-catalog.json",
            auth_service=controller.access,
            orchestrator=provider,
            warehouse_service=None,
            assistant_capability_controller=controller,
            assistant_access_policy=controller.access_policy,
        )
    )
    provider.interpret.assert_not_awaited()


@pytest.mark.parametrize(
    "phrase",
    (
        "что умеет бармен?",
        "какие функции у отчёта?",
        "можешь закрыть смену Дмитро?",
        "что можно сделать с кокосовым молоком?",
        "какие действия были выполнены вчера?",
    ),
)
def test_non_capability_questions_do_not_select_capability_list(
    tmp_path: Path,
    phrase: str,
) -> None:
    result = _controller(tmp_path / "controller.db").dispatch(
        phrase,
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
    )
    assert result is None or result.capability_id != "capabilities.list"


def test_capability_question_can_filter_runtime_registry_by_category(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path / "controller.db")
    result = controller.dispatch(
        "какие действия ты можешь делать со сменами?",
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
    )
    assert result is not None
    assert result.capability_id == "capabilities.list"
    assert "Открыть мою смену" in result.message


def test_registered_bartenders_are_read_from_user_service(tmp_path: Path) -> None:
    controller = _controller(tmp_path / "controller.db")
    result = controller.dispatch(
        "сколько и какие бармены зарегистрированы?",
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
    )
    assert result is not None
    assert result.capability_id == "users.list_bartenders"
    assert "2" in result.message
    assert "Anna" in result.message
    assert "Boris" in result.message


def test_force_close_question_is_not_generic_unavailable(tmp_path: Path) -> None:
    controller = _controller(tmp_path / "controller.db")
    result = controller.dispatch(
        "можешь закрыть смену Дмитро?",
        actor_user_id=10,
        source_chat_id=10,
        source_message_id=1,
    )
    assert result is not None
    assert result.capability_id == "attendance.force_close"
    assert "причин" in result.message.casefold()


def test_role_filtered_discovery_does_not_elevate_accountant(tmp_path: Path) -> None:
    controller = _controller(tmp_path / "controller.db")
    result = controller.dispatch(
        "что ты умеешь?",
        actor_user_id=30,
        source_chat_id=30,
        source_message_id=1,
    )
    assert result is not None
    assert "Изменить роль" not in result.message
    assert "Принудительно закрыть" not in result.message
    assert "Показать историю отчётов" in result.message


def test_full_runtime_binding_matrix_and_ui_routes_are_concrete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "full-controller.db"
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    users.ensure_user(10, first_name="Admin")
    users.set_role(10, Role.ADMIN, actor_user_id=10)
    access = AuthService(frozenset(), users)
    registry = CapabilityRegistry()
    marker = object()
    external = {
        capability_id: CapabilityBinding(
            capability_id,
            marker,
            marker,
            "legacy route",
            "legacy renderer",
            (lambda: None) if capability_id in {"warehouse.commit_batch", "warehouse.cancel_batch"} else None,
        )
        for capability_id in (
            "catalog.search",
            "catalog.get",
            "warehouse.prepare_writeoff",
            "warehouse.prepare_transfer",
            "warehouse.commit_batch",
            "warehouse.cancel_batch",
        )
    }
    proposals = AssistantActionProposalService(
        AssistantActionDatabase(path), access, registry,
        AssistantAccessPolicy(
            access,
            registry,
            Settings(
                _env_file=None, bot_token="test", ai_assistant_enabled=True,
                ai_access_mode="role_based", warehouse_enabled=True,
            ),
        ),
    )
    policy = proposals.access_policy
    controller = AssistantCapabilityController(
        registry,
        proposals,
        access,
        policy,
        flags={"ai_assistant_enabled": True, "warehouse_enabled": True},
        users=users,
        attendance=marker,
        operations=marker,
        closing=marker,
        reports=marker,
        recipients=marker,
        reminders=marker,
        schedules=marker,
        admin=marker,
        correction=marker,
        delivery=marker,
        attendance_xlsx=marker,
        external_bindings=external,
    )
    assert len(controller.bindings) == 41
    assert all(
        binding.executable
        and binding.adapter is controller
        and binding.service is not None
        and binding.route
        and binding.renderer
        for capability_id, binding in controller.bindings.items()
        if capability_id not in external
    )
    assert all(
        callable(binding.executor)
        for capability_id, binding in controller.bindings.items()
        if registry.require(capability_id).risk_level.value >= 2
    )
    cases = {
        "главное меню": ("navigation.main", "main_menu"),
        "управлять каталогом": ("catalog.manage", "catalog_manage"),
        "покажи сотрудника": ("users.get", "users_manage"),
        "измени роль пользователя": ("users.change_role", "users_manage"),
        "сделай табель за текущий месяц": ("attendance_reports.generate", "attendance_xlsx"),
        "запусти опросник обязательных списаний": ("warehouse.start_required_flow", "required_writeoffs"),
        "исправь операцию": ("warehouse.update_operation", "warehouse_operations"),
        "удали операцию": ("warehouse.delete_operation", "warehouse_operations"),
        "закрой общую складскую смену": ("warehouse.close_shift", "warehouse_close"),
        "открой коррекцию закрытого отчёта": ("warehouse.open_correction", "warehouse_corrections"),
        "сформируй сегодняшние списания": ("reports.generate", "report_generate"),
        "статус отчёта": ("reports.status", "reports"),
        "отправь сегодняшние списания в рабочую группу": ("reports.send", "reports"),
        "добавь получателя": ("recipients.create_or_update", "recipients"),
        "отключи получателя": ("recipients.disable", "recipients"),
        "настрой расписание отправки": ("schedules.create_or_update", "schedules"),
        "отключи расписание": ("schedules.disable", "schedules"),
    }
    for text, expected in cases.items():
        dispatch = controller.dispatch(
            text,
            actor_user_id=10,
            source_chat_id=10,
            source_message_id=1,
        )
        assert dispatch is not None
        assert (dispatch.capability_id, dispatch.ui_action) == expected


def test_denied_actor_cannot_discover_tools_or_enter_report_delivery(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path / "denied-controller.db")

    for text in (
        "что ты умеешь?",
        "сделай табель за текущий месяц",
        "отправь сегодняшние списания в рабочую группу",
    ):
        with pytest.raises(AccessDeniedError):
            controller.dispatch(
                text,
                actor_user_id=999,
                source_chat_id=999,
                source_message_id=1,
            )
