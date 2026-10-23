"""Versioned, human-labelled offline conversation contract corpus.

The corpus measures routing contracts and safety. It is not a live-provider
quality claim; live/shadow evidence is tracked separately in KOS.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.assistant.capabilities import CAPABILITIES

CORPUS_VERSION = "bar-jarvis-conversation-v1"


@dataclass(frozen=True, slots=True)
class CorpusTurn:
    text: str
    expected_capability: str | None
    mutation: bool = False
    confirmation: bool = False


@dataclass(frozen=True, slots=True)
class CorpusDialogue:
    identifier: str
    turns: tuple[CorpusTurn, ...]


_PHRASES = {
    "catalog.search": "найди техкарту облепихи",
    "catalog.get": "открой выбранную карточку",
    "catalog.manage": "измени состав выбранной техкарты",
    "warehouse.prepare_writeoff": "спиши апельсин 200 грамм",
    "warehouse.prepare_transfer": "перемести сахар 1 кг на кухню",
    "attendance.list_open": "какие смены сейчас открыты",
    "attendance.force_close": "закрой смену выбранного сотрудника",
    "capabilities.list": "что ты умеешь делать",
    "reports.generate": "сформируй складской отчёт за сегодня",
    "reports.send": "отправь выбранный отчёт получателю",
}


def _dialogue(index: int, capability_id: str, title: str, mutation: bool) -> CorpusDialogue:
    request = _PHRASES.get(capability_id, title.casefold())
    return CorpusDialogue(
        identifier=f"v1-{index:03d}-{capability_id}",
        turns=(
            CorpusTurn(request, capability_id, mutation=mutation),
            CorpusTurn("уточни по последнему выбранному объекту", capability_id, mutation=mutation),
            CorpusTurn("отмена", None, confirmation=False),
        ),
    )


_BASE = tuple(
    _dialogue(index, item.id, item.title, item.risk_level.value != 0)
    for index, item in enumerate(CAPABILITIES, 1)
)
_EXTRA_TOPICS = (
    "местоимение после карточки", "исправление намерения", "неверный род ссылки",
    "первый кандидат", "последний кандидат", "туда же", "перезапуск контекста",
    "изоляция пользователей", "запомни термин", "вспомни предпочтение",
    "забудь точную запись", "неоднозначная память", "provider timeout",
    "provider malformed", "access denied", "role changed", "expired proposal",
    "русско украинская опечатка", "информационный вопрос о списании",
)
_EXTRA = tuple(
    CorpusDialogue(
        identifier=f"v1-extra-{index:03d}",
        turns=(
            CorpusTurn(topic, None),
            CorpusTurn("продолжи по контексту", None),
            CorpusTurn("очисти этот разговор", None),
        ),
    )
    for index, topic in enumerate(_EXTRA_TOPICS, 1)
)

CORPUS = _BASE + _EXTRA
