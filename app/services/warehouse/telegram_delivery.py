"""Injectable Telegram adapters for chat verification and XLSX delivery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.types import FSInputFile


class TelegramChatVerificationError(RuntimeError):
    """Raised when Telegram cannot confirm a writable destination chat."""


@dataclass(frozen=True, slots=True)
class VerifiedTelegramChat:
    chat_id: int
    title: str
    chat_type: str


class ReportSender(Protocol):
    async def send_report(
        self,
        *,
        chat_id: int,
        path: Path,
        filename: str,
        caption: str,
    ) -> int: ...


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


class TelegramChatVerifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def verify(self, chat_id: int) -> VerifiedTelegramChat:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise TelegramChatVerificationError("Нужен числовой Telegram chat ID")
        try:
            chat = await self.bot.get_chat(chat_id)
            chat_type = _enum_value(chat.type)
            if chat_type not in {"private", "group", "supergroup", "channel"}:
                raise TelegramChatVerificationError("Тип чата не поддерживается")
            if chat_type != "private":
                me = await self.bot.get_me()
                member = await self.bot.get_chat_member(chat_id, me.id)
                status = _enum_value(member.status)
                if status in {"left", "kicked"}:
                    raise TelegramChatVerificationError(
                        "Добавьте бота в чат и разрешите отправку документов."
                    )
                if chat_type == "channel":
                    if status not in {"administrator", "creator"} or getattr(
                        member, "can_post_messages", True
                    ) is False:
                        raise TelegramChatVerificationError(
                            "Боту нужны права публикации документов в канале."
                        )
                elif status == "restricted" and getattr(
                    member, "can_send_documents", False
                ) is not True:
                    raise TelegramChatVerificationError(
                        "Разрешите боту отправлять документы в этот чат."
                    )
                elif status == "member" and getattr(
                    getattr(chat, "permissions", None),
                    "can_send_documents",
                    True,
                ) is False:
                    raise TelegramChatVerificationError(
                        "Разрешите участникам и боту отправлять документы в этот чат."
                    )
            title = (
                getattr(chat, "title", None)
                or getattr(chat, "full_name", None)
                or getattr(chat, "username", None)
                or str(chat_id)
            )
            title = str(title).strip()[:200]
            if not title:
                title = str(chat_id)
            return VerifiedTelegramChat(chat_id, title, chat_type)
        except TelegramChatVerificationError:
            raise
        except Exception as exc:
            raise TelegramChatVerificationError(
                "Не удалось проверить чат. Добавьте бота в группу и повторите."
            ) from exc


class TelegramReportSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_report(
        self,
        *,
        chat_id: int,
        path: Path,
        filename: str,
        caption: str,
    ) -> int:
        message = await self.bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(path, filename=filename),
            caption=caption,
        )
        return int(message.message_id)


__all__ = [
    "ReportSender",
    "TelegramChatVerifier",
    "TelegramChatVerificationError",
    "TelegramReportSender",
    "VerifiedTelegramChat",
]
