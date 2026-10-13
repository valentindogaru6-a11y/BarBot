import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.handlers.assistant import AssistantTextEligibleFilter, AssistantVoiceEligibleFilter
from app.models.user import Role


class Policy:
    def __init__(self, role):
        self.role = role

    def can_enter_assistant(self, _user_id, *, chat_type):
        return chat_type == "private" and self.role in {
            Role.SUPER_ADMIN,
            Role.ADMIN,
            Role.BARTENDER,
            Role.ACCOUNTANT,
        }


def message(*, text="hello", chat_type="private"):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(type=chat_type),
    )


def settings(**kwargs):
    return Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=True,
        ai_voice_enabled=True,
        groq_api_key="g",
        gemini_api_key="m",
        **kwargs,
    )


def test_text_ai_requires_private_active_role_and_no_active_fsm() -> None:
    filter_ = AssistantTextEligibleFilter()
    assert asyncio.run(filter_(message(), settings(), Policy(Role.SUPER_ADMIN), None))
    assert not asyncio.run(filter_(message(chat_type="group"), settings(), Policy(Role.SUPER_ADMIN), None))
    assert asyncio.run(filter_(message(), settings(), Policy(Role.ADMIN), None))
    assert asyncio.run(filter_(message(), settings(), Policy(Role.BARTENDER), None))
    assert asyncio.run(filter_(message(), settings(), Policy(Role.ACCOUNTANT), None))
    assert not asyncio.run(filter_(message(), settings(), Policy(Role.BLOCKED), None))
    assert not asyncio.run(filter_(message(), settings(), Policy(Role.SUPER_ADMIN), "active:state"))


@pytest.mark.parametrize("text", ["/start", "/help", "списание:\nморковь 1 кг", "перемещение: молоко 1 л -> кухня"])
def test_commands_and_exact_warehouse_triggers_are_not_intercepted(text) -> None:
    assert not asyncio.run(AssistantTextEligibleFilter()(
        message(text=text), settings(), Policy(Role.SUPER_ADMIN), None
    ))


def test_flags_disable_ai_without_changing_fallback() -> None:
    disabled = Settings(_env_file=None, bot_token="test")
    policy = Policy(Role.SUPER_ADMIN)
    assert not asyncio.run(AssistantTextEligibleFilter()(message(), disabled, policy, None))
    assert not asyncio.run(AssistantVoiceEligibleFilter()(message(), disabled, policy, None))
