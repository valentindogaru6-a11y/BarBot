"""Authorized management of numeric Telegram report recipients."""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any

from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import (
    BusinessDateService,
    Clock,
    utc_rfc3339,
)
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase

_NUMERIC_CHAT_ID = re.compile(r"-?[1-9]\d*\Z")
_CHAT_TYPES = {"private", "group", "supergroup", "channel"}


def parse_numeric_chat_id(value: str) -> int:
    cleaned = value.strip()
    if not _NUMERIC_CHAT_ID.fullmatch(cleaned):
        raise ValueError("Введите числовой chat ID, не @username.")
    return int(cleaned)


class WarehouseRecipientService:
    def __init__(
        self,
        repository: WarehouseDeliveryDatabase,
        access: AccessService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.access = access
        self.clock = clock

    def _now(self) -> str:
        service = BusinessDateService(clock=self.clock)
        return utc_rfc3339(service.clock.now().astimezone(UTC))

    def _authorize(self, actor_user_id: int) -> None:
        self.access.require_warehouse_report_recipient_management(actor_user_id)

    def list_recipients(self, *, actor_user_id: int) -> list[dict[str, Any]]:
        self._authorize(actor_user_id)
        return self.repository.list_recipients()

    def recipient(self, recipient_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self._authorize(actor_user_id)
        recipient = self.repository.recipient(recipient_id)
        if recipient is None:
            raise ValueError("Получатель не найден")
        return recipient

    def active_count(self, *, actor_user_id: int) -> int:
        self.access.require_warehouse_report_delivery(actor_user_id)
        return self.repository.active_count()

    def create_verified(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._authorize(actor_user_id)
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise ValueError("Нужен числовой Telegram chat ID")
        cleaned_title = title.strip()
        if not cleaned_title or len(cleaned_title) > 200:
            raise ValueError("Название должно содержать от 1 до 200 символов")
        if chat_type not in _CHAT_TYPES:
            raise ValueError("Неподдерживаемый тип Telegram-чата")
        return self.repository.create_recipient(
            chat_id=chat_id,
            title=cleaned_title,
            chat_type=chat_type,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._authorize(actor_user_id),
        )

    def rename(
        self,
        recipient_id: int,
        title: str,
        *,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._authorize(actor_user_id)
        cleaned = title.strip()
        if not cleaned or len(cleaned) > 200:
            raise ValueError("Название должно содержать от 1 до 200 символов")
        return self.repository.update_title(
            recipient_id,
            title=cleaned,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._authorize(actor_user_id),
        )

    def set_active(
        self,
        recipient_id: int,
        *,
        is_active: bool,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._authorize(actor_user_id)
        return self.repository.set_active(
            recipient_id,
            is_active=is_active,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._authorize(actor_user_id),
        )


__all__ = ["WarehouseRecipientService", "parse_numeric_chat_id"]
