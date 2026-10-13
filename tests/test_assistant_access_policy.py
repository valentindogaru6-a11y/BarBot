from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.assistant_capabilities import CapabilityRisk
from app.models.user import Role
from app.services.assistant import (
    AssistantAccessDenied,
    AssistantAccessPolicy,
    CapabilityBinding,
    CapabilityRegistry,
)
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.storage.user_database import UserDatabase


def _settings(**updates) -> Settings:
    values = {
        "_env_file": None,
        "bot_token": "test",
        "warehouse_enabled": True,
        "ai_assistant_enabled": True,
        "ai_access_mode": "pilot_allowlist",
        "ai_pilot_user_ids": "1,2,3,4,6",
    }
    values.update(updates)
    return Settings(**values)


def _environment(path: Path, settings: Settings):
    database = UserDatabase(path)
    database.initialize()
    users = UserService(database)
    for user_id, role in (
        (1, Role.USER),
        (2, Role.ADMIN),
        (3, Role.BARTENDER),
        (4, Role.ACCOUNTANT),
        (5, Role.USER),
        (6, Role.BLOCKED),
    ):
        users.ensure_user(user_id)
        users.set_role(user_id, role, actor_user_id=1)
    access = AuthService(frozenset({1}), users)
    registry = CapabilityRegistry()
    marker = object()
    executor = lambda: None
    registry.bind(
        {
            capability.id: CapabilityBinding(
                capability.id,
                marker,
                marker,
                "test route",
                "test renderer",
                executor
                if capability.risk_level >= CapabilityRisk.MUTATION
                else None,
            )
            for capability in registry.all
        }
    )
    return users, access, registry, AssistantAccessPolicy(access, registry, settings)


def test_config_default_and_fail_closed_allowlist_validation() -> None:
    disabled = Settings(_env_file=None, bot_token="test")
    assert disabled.ai_access_mode == "pilot_allowlist"
    assert disabled.ai_pilot_user_ids == frozenset()

    with pytest.raises(ValidationError, match="AI_PILOT_USER_IDS"):
        Settings(_env_file=None, bot_token="test", ai_assistant_enabled=True)
    for value in ("invalid-sensitive-value", "0", "-1", "[1, false]"):
        with pytest.raises(ValidationError) as caught:
            Settings(
                _env_file=None,
                bot_token="test",
                ai_assistant_enabled=True,
                ai_pilot_user_ids=value,
            )
        if "sensitive" in value:
            assert value not in str(caught.value)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            bot_token="test",
            ai_assistant_enabled=True,
            ai_access_mode="unknown",
        )


def test_config_normalizes_whitespace_duplicates_and_role_mode() -> None:
    pilot = _settings(ai_pilot_user_ids=" 1, 1, 2 ")
    assert pilot.ai_pilot_user_ids == frozenset({1, 2})
    role_based = Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
    )
    assert role_based.ai_pilot_user_ids == frozenset()


def test_pilot_requires_active_allowlisted_super_admin_private_chat(
    tmp_path: Path,
) -> None:
    users, _, _, policy = _environment(tmp_path / "pilot.db", _settings())
    assert policy.can_enter_assistant(1, chat_type="private")
    assert not policy.can_enter_assistant(1, chat_type="group")
    assert not policy.can_enter_assistant(2, chat_type="private")
    assert not policy.can_enter_assistant(3, chat_type="private")
    assert not policy.can_enter_assistant(4, chat_type="private")
    assert not policy.can_enter_assistant(5, chat_type="private")
    assert not policy.can_enter_assistant(6, chat_type="private")
    users.ensure_user(7)
    assert not policy.can_enter_assistant(7, chat_type="private")

    with users.database._connect() as connection:
        connection.execute("UPDATE users SET is_active=0 WHERE telegram_user_id=1")
    assert not policy.can_enter_assistant(1, chat_type="private")


def test_role_based_mode_preserves_registry_and_access_filtering(
    tmp_path: Path,
) -> None:
    settings = _settings(ai_access_mode="role_based", ai_pilot_user_ids="")
    _, _, registry, policy = _environment(tmp_path / "roles.db", settings)
    available = {
        actor: {item.id for item in policy.available_capabilities(actor)}
        for actor in range(1, 7)
    }
    assert len(available[1]) == len(registry.all) == 41
    assert "users.change_role" in available[2]
    assert "users.change_role" not in available[3]
    assert "catalog.search" not in available[4]
    assert "warehouse.prepare_writeoff" not in available[4]
    assert available[5] == set()
    assert available[6] == set()


def test_policy_rechecks_mode_allowlist_role_active_state_and_flags(
    tmp_path: Path,
) -> None:
    settings = _settings()
    users, _, _, policy = _environment(tmp_path / "recheck.db", settings)
    assert len(policy.available_capabilities(1)) == 41

    settings.ai_pilot_user_ids = frozenset()
    with pytest.raises(AssistantAccessDenied):
        policy.require_assistant_access(1, chat_type="private")
    settings.ai_pilot_user_ids = frozenset({1, 2, 3, 4, 6})

    users.block_user(1, actor_user_id=1)
    with pytest.raises(AssistantAccessDenied):
        policy.require_assistant_access(1, chat_type="private")
    settings.ai_access_mode = "role_based"
    assert policy.available_capabilities(2)
    users.block_user(2, actor_user_id=1)
    assert policy.available_capabilities(2) == ()
    settings.ai_assistant_enabled = False
    assert policy.available_capabilities(3) == ()
