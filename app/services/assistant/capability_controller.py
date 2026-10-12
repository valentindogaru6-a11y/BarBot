"""Deterministic application adapters for high-confidence AI capabilities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.models.assistant_capabilities import ActionProposal
from app.models.user import Role
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.proposals import (
    AssistantActionProposalService,
    AssistantProposalTargetConflict,
)
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.schedule_service import WarehouseScheduleService


@dataclass(frozen=True, slots=True)
class CapabilityDispatch:
    capability_id: str
    message: str
    proposal: ActionProposal | None = None


_CAPABILITY_QUESTION = re.compile(
    r"\b(?:что\s+ты\s+умеешь|что\s+ты\s+можешь|какие\s+(?:действия|функции)|"
    r"что\s+можешь\s+сделать)\b",
    re.IGNORECASE,
)
_BARTENDERS = re.compile(r"\b(?:сколько|какие|покажи|список)\b.*\bбармен\w*\b", re.IGNORECASE)
_USERS = re.compile(r"\b(?:покажи|список|какие)\b.*\b(?:сотрудник\w*|пользовател\w*)\b", re.IGNORECASE)
_OPEN_OWN = re.compile(r"\bоткрой\w*\b.*\b(?:мою|мне)\b.*\bсмен\w*\b|\bоткрой\w*\s+смен\w*\s+(?:мне|сегодня)\b", re.IGNORECASE)
_CLOSE_OWN = re.compile(r"\bзакрой\w*\b.*\bмою\b.*\bсмен\w*\b", re.IGNORECASE)
_FORCE_CLOSE = re.compile(r"\bзакр(?:ой|ыть)\w*\b.*\bсмен\w*\b\s+(.+)", re.IGNORECASE)
_REMINDER_FIRST = re.compile(r"\bперв\w*\s+напоминан\w*\b.*?\b(\d{1,4})\s*мин", re.IGNORECASE)
_REPORT_HISTORY = re.compile(r"\b(?:последн\w*|истори\w*)\b.*\bотч[её]т\w*\b", re.IGNORECASE)
_RECIPIENTS = re.compile(r"\b(?:кто\s+получает|получател\w*)\b.*\bотч[её]т\w*\b", re.IGNORECASE)
_SCHEDULES = re.compile(r"\b(?:расписан\w*|автоотправ\w*)\b", re.IGNORECASE)
_OPERATIONS = re.compile(r"\b(?:запис\w*|операци\w*)\b.*\b(?:текущ\w*\s+)?смен\w*\b", re.IGNORECASE)
_REASON_SPLIT = re.compile(r"\s+(?:потому\s+что|причина\s*[:—-]?|так\s+как)\s+", re.IGNORECASE)


class AssistantCapabilityController:
    """Routes deterministic phrases to services without fake Telegram updates."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        proposals: AssistantActionProposalService,
        access: AccessService,
        *,
        flags: dict[str, bool],
        users: UserService | None = None,
        attendance: WarehouseAttendanceService | None = None,
        operations: WarehouseOperationsService | None = None,
        closing: WarehouseClosingService | None = None,
        reports: WarehouseReportService | None = None,
        recipients: WarehouseRecipientService | None = None,
        reminders: WarehouseReminderSettingsService | None = None,
        schedules: WarehouseScheduleService | None = None,
    ) -> None:
        self.registry = registry
        self.proposals = proposals
        self.access = access
        self.flags = flags
        self.users = users
        self.attendance = attendance
        self.operations = operations
        self.closing = closing
        self.reports = reports
        self.recipients = recipients
        self.reminders = reminders
        self.schedules = schedules

    def _require(self, actor: int, capability_id: str) -> None:
        capability = self.registry.require(capability_id)
        role = self.access.get_context(actor).role
        available = self.registry.available(role=role, flags=self.flags)
        if capability not in available:
            raise AccessDeniedError("Capability is unavailable")

    def _proposal(
        self,
        capability_id: str,
        message: str,
        *,
        actor: int,
        chat: int,
        message_id: int,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        target_type: str | None = None,
        target_id: str | None = None,
        target_revision: int | None = None,
        reason: str | None = None,
    ) -> CapabilityDispatch:
        proposal = self.proposals.create(
            actor_user_id=actor,
            source_chat_id=chat,
            source_message_id=message_id,
            capability_id=capability_id,
            arguments=arguments,
            preview=preview,
            target_type=target_type,
            target_id=target_id,
            target_revision=target_revision,
            reason=reason,
        )
        return CapabilityDispatch(capability_id, message, proposal)

    def _capabilities(self, actor: int, query: str) -> CapabilityDispatch:
        role = self.access.get_context(actor).role
        groups = self.registry.describe(role=role, flags=self.flags)
        normalized = query.casefold()
        category_words = {
            "смен": {"attendance", "warehouse"},
            "отч": {"reports", "recipients"},
            "сотруд": {"employees"},
            "напомин": {"reminders", "schedules"},
            "каталог": {"catalog"},
        }
        selected = next((value for word, value in category_words.items() if word in normalized), None)
        labels = {
            "attendance": "Смены", "warehouse": "Склад", "reports": "Отчёты",
            "recipients": "Получатели", "employees": "Сотрудники",
            "reminders": "Напоминания", "schedules": "Расписания",
            "catalog": "Каталог", "navigation": "Навигация", "help": "Помощь",
        }
        lines = ["Доступные вам функции:"]
        for category, capabilities in groups.items():
            if selected is not None and category not in selected:
                continue
            lines.append(f"\n{labels.get(category, category.title())}:")
            lines.extend(f"• {item.title}" for item in capabilities)
        return CapabilityDispatch("capabilities.list", "\n".join(lines))

    def dispatch(
        self,
        text: str,
        *,
        actor_user_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> CapabilityDispatch | None:
        cleaned = " ".join(text.split())
        if _CAPABILITY_QUESTION.search(cleaned):
            return self._capabilities(actor_user_id, cleaned)
        if _BARTENDERS.search(cleaned):
            self._require(actor_user_id, "users.list_bartenders")
            if self.users is None:
                return CapabilityDispatch("users.list_bartenders", "Список барменов временно недоступен.")
            users, _, _ = self.users.list_users(role=Role.BARTENDER, page_size=100)
            lines = [f"Зарегистрировано барменов: {len(users)}."]
            lines.extend(f"• {user.display_name}" for user in users)
            return CapabilityDispatch("users.list_bartenders", "\n".join(lines))
        if _USERS.search(cleaned):
            self._require(actor_user_id, "users.list")
            if self.users is None:
                return CapabilityDispatch("users.list", "Список сотрудников временно недоступен.")
            users, _, _ = self.users.list_users(page_size=100)
            lines = [f"Зарегистрировано сотрудников: {len(users)}."]
            lines.extend(f"• {user.display_name} — {user.role.value}" for user in users)
            return CapabilityDispatch("users.list", "\n".join(lines))
        if _OPEN_OWN.search(cleaned):
            self._require(actor_user_id, "attendance.open_own")
            if self.attendance is None:
                return CapabilityDispatch("attendance.open_own", "Учёт смен временно недоступен.")
            active = self.attendance.status(actor_user_id)
            if active is not None:
                return CapabilityDispatch("attendance.status", "Ваша персональная смена уже открыта.")
            return self._proposal(
                "attendance.open_own", "Открыть вашу персональную смену сейчас?",
                actor=actor_user_id, chat=source_chat_id, message_id=source_message_id,
                arguments={}, preview={"action": "open_own_attendance"},
            )
        if _CLOSE_OWN.search(cleaned):
            self._require(actor_user_id, "attendance.close_own")
            if self.attendance is None:
                return CapabilityDispatch("attendance.close_own", "Учёт смен временно недоступен.")
            active = self.attendance.status(actor_user_id)
            if active is None:
                return CapabilityDispatch("attendance.status", "У вас нет открытой персональной смены.")
            revision = int(active["revision"])
            return self._proposal(
                "attendance.close_own", "Закрыть вашу персональную смену?",
                actor=actor_user_id, chat=source_chat_id, message_id=source_message_id,
                arguments={}, preview={"action": "close_own_attendance"},
                target_type="attendance_session", target_id=str(active["id"]),
                target_revision=revision,
            )
        force = _FORCE_CLOSE.search(cleaned)
        if force and "мою" not in cleaned.casefold() and "общ" not in cleaned.casefold():
            self._require(actor_user_id, "attendance.force_close")
            if cleaned.casefold().startswith("можешь"):
                return CapabilityDispatch(
                    "attendance.force_close",
                    "Да. Для принудительного закрытия чужой смены укажите сотрудника и обязательную причину.",
                )
            if self.attendance is None:
                return CapabilityDispatch("attendance.force_close", "Учёт смен временно недоступен.")
            parts = _REASON_SPLIT.split(force.group(1), maxsplit=1)
            target = parts[0].strip(" ?.,")
            if len(parts) != 2 or not parts[1].strip():
                return CapabilityDispatch(
                    "attendance.force_close",
                    f"Укажите причину принудительного закрытия смены {target}.",
                )
            reason = parts[1].strip()
            matches = [
                row for row in self.attendance.open_sessions(actor_user_id)
                if target.casefold() in str(row.get("display_name_snapshot", "")).casefold()
            ]
            if len(matches) != 1:
                return CapabilityDispatch(
                    "attendance.force_close",
                    "Не удалось однозначно определить открытую смену. Уточните сотрудника.",
                )
            session = matches[0]
            label = str(session.get("display_name_snapshot") or "Сотрудник")
            return self._proposal(
                "attendance.force_close",
                f"Принудительно закрыть смену «{label}»?\nПричина: {reason}",
                actor=actor_user_id, chat=source_chat_id, message_id=source_message_id,
                arguments={"target_ref": str(session["id"]), "expected_revision": int(session["revision"]), "reason": reason},
                preview={"employee": label, "reason": reason},
                target_type="attendance_session", target_id=str(session["id"]),
                target_revision=int(session["revision"]), reason=reason,
            )
        reminder = _REMINDER_FIRST.search(cleaned)
        if reminder:
            self._require(actor_user_id, "reminders.update")
            if self.reminders is None or self.operations is None:
                return CapabilityDispatch("reminders.update", "Настройки напоминаний временно недоступны.")
            minutes = int(reminder.group(1))
            shift = self.operations.current_shift(actor_user_id=actor_user_id)
            source_id = int(shift["source_location_id"])
            current = self.reminders.get(source_id, actor_user_id)
            return self._proposal(
                "reminders.update",
                f"Изменить первое напоминание: {current['first_reminder_minutes_before_close']} → {minutes} минут?",
                actor=actor_user_id, chat=source_chat_id, message_id=source_message_id,
                arguments={"target_ref": str(source_id), "expected_revision": int(current["revision"]), "values": {"first": minutes}},
                preview={"before": int(current["first_reminder_minutes_before_close"]), "after": minutes},
                target_type="reminder_settings", target_id=str(source_id),
                target_revision=int(current["revision"]),
            )
        if _REPORT_HISTORY.search(cleaned):
            self._require(actor_user_id, "reports.history")
            if self.reports is None:
                return CapabilityDispatch("reports.history", "История отчётов временно недоступна.")
            rows, total = self.reports.history(actor_user_id=actor_user_id, page=0)
            lines = [f"Готовых отчётов: {total}."]
            lines.extend(f"• {row['business_date']} — версия {row['version']}" for row in rows)
            return CapabilityDispatch("reports.history", "\n".join(lines))
        if _RECIPIENTS.search(cleaned):
            self._require(actor_user_id, "recipients.list")
            if self.recipients is None:
                return CapabilityDispatch("recipients.list", "Получатели временно недоступны.")
            rows = self.recipients.list_recipients(actor_user_id=actor_user_id)
            lines = [f"Получателей отчётов: {len(rows)}."]
            lines.extend(f"• {row['title']} — {'активен' if row['is_active'] else 'отключён'}" for row in rows)
            return CapabilityDispatch("recipients.list", "\n".join(lines))
        if _SCHEDULES.search(cleaned) and "настрой" not in cleaned.casefold():
            self._require(actor_user_id, "schedules.list")
            if self.schedules is None:
                return CapabilityDispatch("schedules.list", "Расписания временно недоступны.")
            rows = self.schedules.list(actor_user_id)
            return CapabilityDispatch("schedules.list", f"Настроено расписаний: {len(rows)}.")
        if _OPERATIONS.search(cleaned):
            self._require(actor_user_id, "warehouse.list_operations")
            if self.operations is None:
                return CapabilityDispatch("warehouse.list_operations", "Склад временно недоступен.")
            shift = self.operations.current_shift(actor_user_id=actor_user_id)
            rows = [] if shift["id"] is None else self.operations.list_operations(int(shift["id"]), actor_user_id=actor_user_id)
            return CapabilityDispatch("warehouse.list_operations", f"Записей в текущей смене: {len(rows)}.")
        return None

    def execute(self, row: dict[str, Any], actor_user_id: int) -> dict[str, Any]:
        capability = str(row["capability_id"])
        arguments = dict(row["arguments"])
        if capability == "attendance.open_own":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            session, created = self.attendance.open(actor_user_id)
            return {"status": "opened" if created else "already_open", "revision": int(session["revision"])}
        if capability == "attendance.close_own":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            active = self.attendance.status(actor_user_id)
            if active is None or str(active["id"]) != str(row.get("target_id")) or int(active["revision"]) != int(row.get("target_revision")):
                raise AssistantProposalTargetConflict("Attendance changed")
            session, closed = self.attendance.close(actor_user_id)
            return {"status": "closed" if closed else "already_closed", "revision": int(session["revision"]) if session else 0}
        if capability == "attendance.force_close":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            result = self.attendance.force_close(
                int(arguments["target_ref"]), actor_user_id,
                str(arguments["reason"]), int(arguments["expected_revision"]),
            )
            return {"status": "force_closed", "revision": int(result["revision"])}
        if capability == "reminders.update":
            if self.reminders is None:
                raise RuntimeError("Reminder settings unavailable")
            result = self.reminders.update(
                int(arguments["target_ref"]), dict(arguments["values"]),
                int(row["target_revision"]), actor_user_id,
            )
            return {"status": "updated", "revision": int(result["revision"])}
        raise ValueError("Capability executor is not allowlisted")
