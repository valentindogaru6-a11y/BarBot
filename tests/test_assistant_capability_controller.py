from pathlib import Path

from app.models.user import Role
from app.services.auth_service import AuthService
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.capability_controller import AssistantCapabilityController
from app.services.assistant.proposals import AssistantActionProposalService
from app.services.user_service import UserService
from app.storage.assistant_action_database import AssistantActionDatabase
from app.storage.user_database import UserDatabase


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
    proposals = AssistantActionProposalService(
        AssistantActionDatabase(path), access, registry
    )
    return AssistantCapabilityController(
        registry,
        proposals,
        access,
        flags={"ai_assistant_enabled": True, "warehouse_enabled": True},
        users=users,
    )


def test_capability_question_does_not_run_open_sessions(tmp_path: Path) -> None:
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
