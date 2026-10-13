"""Deterministic application adapters for high-confidence AI capabilities."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.models.assistant_capabilities import ActionProposal
from app.models.user import Role
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.capabilities import CapabilityBinding, CapabilityRegistry
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.services.assistant.proposals import (
    AssistantActionProposalService,
    AssistantProposalCollision,
    AssistantProposalTargetConflict,
)
from app.services.user_service import UserService
from app.services.admin_service import AdminService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.schedule_service import WarehouseScheduleService
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.attendance_xlsx_service import WarehouseAttendanceXlsxService


@dataclass(frozen=True, slots=True)
class CapabilityDispatch:
    capability_id: str
    message: str
    proposal: ActionProposal | None = None
    ui_action: str | None = None
    data: dict[str, Any] | None = None


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
_MAIN_MENU = re.compile(r"^(?:главное\s+меню|открой\s+меню|меню)$", re.IGNORECASE)
_OWN_STATUS = re.compile(r"\b(?:моя|мою)\b.*\bсмен\w*\b.*\b(?:статус|открыт|ид[её]т)\w*\b|\bстатус\b.*\b(?:моей|моя)\b.*\bсмен", re.IGNORECASE)
_OPEN_SESSIONS = re.compile(r"\b(?:покажи|какие|чьи)\b.*\bоткрыт\w*\b.*\bсмен\w*\b", re.IGNORECASE)
_SHIFT_SUMMARY = re.compile(r"\b(?:сводк\w*|итог\w*|что)\b.*\b(?:текущ\w*\s+)?(?:складск\w*\s+)?смен\w*\b", re.IGNORECASE)
_REQUIRED_STATUS = re.compile(r"\b(?:статус|какие|покажи)\b.*\bобязательн\w*\b.*\bсписан\w*\b", re.IGNORECASE)
_REMINDER_SETTINGS = re.compile(r"\b(?:покажи|какие|настройк\w*)\b.*\bнапоминан\w*\b", re.IGNORECASE)
_CATALOG_MANAGE = re.compile(r"\b(?:управ\w*|редактир\w*)\b.*\bкаталог\w*\b", re.IGNORECASE)
_USER_GET = re.compile(r"\b(?:покажи|найди)\b.*\b(?:сотрудник\w*|пользовател\w*)\b", re.IGNORECASE)
_USER_ROLE = re.compile(r"\b(?:измени|назначь|поменяй|заблокир\w*)\b.*\b(?:роль|админ\w*|бармен\w*|бухгалтер\w*|пользовател\w*)\b", re.IGNORECASE)
_ATTENDANCE_GET = re.compile(r"\b(?:покажи|найди)\b.*\bсмен\w*\b.*\bсотрудник\w*\b", re.IGNORECASE)
_ATTENDANCE_XLSX = re.compile(r"\b(?:сделай|сформируй|покажи)\b.*\bтабел\w*\b", re.IGNORECASE)
_REQUIRED_START = re.compile(r"\b(?:запусти|начни|пройди)\b.*\b(?:опросник\w*|обязательн\w*\s+списан\w*)\b", re.IGNORECASE)
_OPERATION_UPDATE = re.compile(r"\b(?:исправь|измени|редактир\w*)\b.*\b(?:операци\w*|запис\w*)\b", re.IGNORECASE)
_OPERATION_DELETE = re.compile(r"\b(?:удали|удалить)\b.*\b(?:операци\w*|запис\w*)\b", re.IGNORECASE)
_WAREHOUSE_CLOSE = re.compile(r"\bзакр\w*\b.*\b(?:общ\w*|складск\w*)\b.*\bсмен\w*\b", re.IGNORECASE)
_CORRECTION = re.compile(r"\b(?:коррекц\w*|исправ\w*)\b.*\b(?:закрыт\w*\s+смен\w*|отч[её]т\w*)\b", re.IGNORECASE)
_REPORT_GENERATE = re.compile(r"\b(?:сформируй|сделай|создай)\b.*\b(?:сегодняшн\w*\s+списан\w*|складск\w*\s+отч[её]т\w*|xlsx|excel)\b", re.IGNORECASE)
_REPORT_STATUS = re.compile(r"\bстатус\b.*\bотч[её]т\w*\b", re.IGNORECASE)
_REPORT_SEND = re.compile(r"\bотправ\w*\b.*\b(?:списан\w*|отч[её]т\w*)\b", re.IGNORECASE)
_RECIPIENT_MUTATION = re.compile(r"\b(?:настрой|добавь|измени|отключи)\b.*\bполучател\w*\b", re.IGNORECASE)
_SCHEDULE_MUTATION = re.compile(r"\b(?:настрой|создай|измени|отключи)\b.*\b(?:расписан\w*|автоотправ\w*)\b", re.IGNORECASE)
_BATCH_COMMIT = re.compile(r"\b(?:подтверди|заверши)\b.*\b(?:складск\w*\s+)?(?:операци\w*|черновик\w*|draft)\b", re.IGNORECASE)
_BATCH_CANCEL = re.compile(r"\b(?:отмени|удали)\b.*\b(?:складск\w*\s+)?(?:черновик\w*|draft)\b", re.IGNORECASE)
_REASON_SPLIT = re.compile(r"\s+(?:потому\s+что|причина\s*[:—-]?|так\s+как)\s+", re.IGNORECASE)


class AssistantCapabilityController:
    """Routes deterministic phrases to services without fake Telegram updates."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        proposals: AssistantActionProposalService,
        access: AccessService,
        access_policy: AssistantAccessPolicy,
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
        admin: AdminService | None = None,
        correction: WarehouseCorrectionService | None = None,
        delivery: WarehouseReportDeliveryService | None = None,
        attendance_xlsx: WarehouseAttendanceXlsxService | None = None,
        external_bindings: dict[str, CapabilityBinding] | None = None,
    ) -> None:
        self.registry = registry
        self.proposals = proposals
        self.access = access
        self.access_policy = access_policy
        self.flags = flags
        self.users = users
        self.attendance = attendance
        self.operations = operations
        self.closing = closing
        self.reports = reports
        self.recipients = recipients
        self.reminders = reminders
        self.schedules = schedules
        self.admin = admin
        self.correction = correction
        self.delivery = delivery
        self.attendance_xlsx = attendance_xlsx
        bindings = dict(external_bindings or {})

        def bind(
            capability_id: str,
            service: object | None,
            *,
            mutation: bool = False,
            ui_flow_mutation: bool = False,
        ) -> None:
            if service is None:
                return
            bindings[capability_id] = CapabilityBinding(
                capability_id=capability_id,
                adapter=self,
                service=service,
                route=(
                    "typed Telegram callback/FSM"
                    if ui_flow_mutation
                    else "AssistantCapabilityController.dispatch"
                ),
                renderer="CapabilityDispatch",
                executor=(
                    self.dispatch
                    if ui_flow_mutation
                    else self.execute if mutation else None
                ),
            )

        bind("capabilities.list", registry)
        bind("navigation.main", registry)
        bind("users.list", users)
        bind("users.list_bartenders", users)
        bind("users.get", users)
        bind("users.change_role", users, ui_flow_mutation=True)
        bind("catalog.manage", admin, ui_flow_mutation=True)
        bind("attendance.status", attendance)
        bind("attendance.list_open", attendance)
        bind("attendance.get", attendance)
        bind("attendance.open_own", attendance, mutation=True)
        bind("attendance.close_own", attendance, mutation=True)
        bind("attendance.force_close", attendance, mutation=True)
        bind("attendance_reports.generate", attendance_xlsx)
        bind("reports.history", reports)
        bind("reports.generate", reports)
        bind("reports.status", reports)
        bind("reports.send", delivery, ui_flow_mutation=True)
        bind("recipients.list", recipients)
        bind("recipients.create_or_update", recipients, ui_flow_mutation=True)
        bind("recipients.disable", recipients, ui_flow_mutation=True)
        bind("reminders.update", reminders, mutation=True)
        bind("reminders.get", reminders if operations is not None else None)
        bind("schedules.list", schedules)
        bind("schedules.create_or_update", schedules, ui_flow_mutation=True)
        bind("schedules.disable", schedules, ui_flow_mutation=True)
        bind("warehouse.shift_summary", operations)
        bind("warehouse.list_operations", operations)
        bind("warehouse.required_status", closing)
        bind("warehouse.start_required_flow", closing)
        bind("warehouse.close_shift", closing, ui_flow_mutation=True)
        bind("warehouse.update_operation", operations, ui_flow_mutation=True)
        bind("warehouse.delete_operation", operations, ui_flow_mutation=True)
        bind("warehouse.open_correction", correction, ui_flow_mutation=True)
        bind("warehouse.commit_correction", correction, ui_flow_mutation=True)
        registry.bind(bindings)

    @property
    def bindings(self) -> dict[str, CapabilityBinding]:
        return dict(self.registry.bindings)

    def available_ids(self, actor_user_id: int) -> tuple[str, ...]:
        return tuple(
            item.id
            for item in self.access_policy.available_capabilities(actor_user_id)
        )

    def _require(self, actor: int, capability_id: str) -> None:
        capability = self.registry.require(capability_id)
        available = self.access_policy.available_capabilities(actor)
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
        try:
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
        except AssistantProposalCollision as exc:
            title = self.registry.require(exc.active.capability_id).title
            return CapabilityDispatch(
                exc.active.capability_id,
                f"У вас уже есть незавершённое действие: {title}.\n"
                "Продолжите его или отмените перед созданием нового.",
                exc.active,
            )
        return CapabilityDispatch(capability_id, message, proposal)

    def _capabilities(self, actor: int, query: str) -> CapabilityDispatch:
        available = self.access_policy.available_capabilities(actor)
        groups: dict[str, tuple[Any, ...]] = {}
        for category in sorted({item.category for item in available}):
            groups[category] = tuple(
                sorted(
                    (item for item in available if item.category == category),
                    key=lambda item: item.title.casefold(),
                )
            )
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

        def flow(capability_id: str, message: str, action: str) -> CapabilityDispatch:
            self._require(actor_user_id, capability_id)
            return CapabilityDispatch(capability_id, message, ui_action=action)

        if _MAIN_MENU.fullmatch(cleaned):
            self._require(actor_user_id, "navigation.main")
            return CapabilityDispatch(
                "navigation.main", "Главное меню", ui_action="main_menu"
            )
        if _CAPABILITY_QUESTION.search(cleaned):
            self._require(actor_user_id, "capabilities.list")
            return self._capabilities(actor_user_id, cleaned)
        if _CATALOG_MANAGE.search(cleaned):
            return flow("catalog.manage", "Откройте штатное управление каталогом.", "catalog_manage")
        if _USER_ROLE.search(cleaned):
            return flow("users.change_role", "Выберите сотрудника и новую роль в штатном интерфейсе.", "users_manage")
        if _USER_GET.search(cleaned):
            return flow("users.get", "Откройте поиск или список сотрудников.", "users_manage")
        if _ATTENDANCE_XLSX.search(cleaned):
            return flow("attendance_reports.generate", "Выберите период табеля.", "attendance_xlsx")
        if _ATTENDANCE_GET.search(cleaned):
            self._require(actor_user_id, "attendance.get")
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            rows = self.attendance.open_sessions(actor_user_id)
            if not rows:
                return CapabilityDispatch("attendance.get", "Открытых персональных смен нет.")
            lines = ["Открытые персональные смены:"]
            lines.extend(f"• {row['display_name_snapshot']}" for row in rows)
            return CapabilityDispatch("attendance.get", "\n".join(lines))
        if _REQUIRED_START.search(cleaned):
            return flow("warehouse.start_required_flow", "Продолжите штатный опрос обязательных списаний.", "required_writeoffs")
        if _BATCH_COMMIT.search(cleaned) or _BATCH_CANCEL.search(cleaned):
            capability_id = (
                "warehouse.cancel_batch" if _BATCH_CANCEL.search(cleaned)
                else "warehouse.commit_batch"
            )
            self._require(actor_user_id, capability_id)
            if self.operations is None:
                raise RuntimeError("Warehouse unavailable")
            batch = self.operations.pending_batch(actor_user_id=actor_user_id)
            if batch is None:
                return CapabilityDispatch(capability_id, "Активного складского черновика нет.")
            return CapabilityDispatch(
                capability_id,
                "Проверьте активный складской черновик.",
                ui_action="warehouse_batch",
                data={"batch_id": int(batch["id"])},
            )
        if _OPERATION_DELETE.search(cleaned):
            return flow("warehouse.delete_operation", "Выберите запись и подтвердите удаление с причиной.", "warehouse_operations")
        if _OPERATION_UPDATE.search(cleaned):
            return flow("warehouse.update_operation", "Выберите запись для исправления.", "warehouse_operations")
        if _WAREHOUSE_CLOSE.search(cleaned):
            return flow("warehouse.close_shift", "Перейдите к штатной проверке и закрытию смены.", "warehouse_close")
        if _CORRECTION.search(cleaned):
            capability_id = "warehouse.commit_correction" if re.search(r"\b(?:выпусти|заверши|подтверди)\b", cleaned, re.IGNORECASE) else "warehouse.open_correction"
            return flow(capability_id, "Выберите закрытый отчёт и продолжите штатную корректировку.", "warehouse_corrections")
        if _REPORT_SEND.search(cleaned):
            return flow("reports.send", "Выберите готовый отчёт и настроенного получателя.", "reports")
        if _REPORT_GENERATE.search(cleaned):
            return flow("reports.generate", "Сформировать предварительный XLSX текущей смены.", "report_generate")
        if _REPORT_STATUS.search(cleaned):
            return flow("reports.status", "Выберите отчёт для просмотра статуса.", "reports")
        if _RECIPIENT_MUTATION.search(cleaned):
            capability_id = "recipients.disable" if re.search(r"\bотключ", cleaned, re.IGNORECASE) else "recipients.create_or_update"
            return flow(capability_id, "Продолжите настройку получателей в штатном интерфейсе.", "recipients")
        if _SCHEDULE_MUTATION.search(cleaned):
            capability_id = "schedules.disable" if re.search(r"\bотключ", cleaned, re.IGNORECASE) else "schedules.create_or_update"
            return flow(capability_id, "Продолжите настройку расписания в штатном интерфейсе.", "schedules")
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
        if _OWN_STATUS.search(cleaned):
            self._require(actor_user_id, "attendance.status")
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            active = self.attendance.status(actor_user_id)
            if active is None:
                return CapabilityDispatch("attendance.status", "Ваша персональная смена закрыта.")
            return CapabilityDispatch("attendance.status", "Ваша персональная смена открыта.")
        if _OPEN_SESSIONS.search(cleaned):
            self._require(actor_user_id, "attendance.list_open")
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            rows = self.attendance.open_sessions(actor_user_id)
            lines = [f"Открытых персональных смен: {len(rows)}."]
            lines.extend(f"• {row['display_name_snapshot']}" for row in rows)
            return CapabilityDispatch("attendance.list_open", "\n".join(lines))
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
        if _REMINDER_SETTINGS.search(cleaned):
            self._require(actor_user_id, "reminders.get")
            if self.reminders is None or self.operations is None:
                raise RuntimeError("Reminder settings unavailable")
            shift = self.operations.current_shift(actor_user_id=actor_user_id)
            row = self.reminders.get(int(shift["source_location_id"]), actor_user_id)
            return CapabilityDispatch(
                "reminders.get",
                "Настройки напоминаний: первое за "
                f"{int(row['first_reminder_minutes_before_close'])} мин., "
                f"повтор каждые {int(row['repeat_interval_minutes'])} мин.",
            )
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
        if _SHIFT_SUMMARY.search(cleaned):
            self._require(actor_user_id, "warehouse.shift_summary")
            if self.operations is None:
                raise RuntimeError("Warehouse unavailable")
            shift = self.operations.current_shift(actor_user_id=actor_user_id)
            if shift["id"] is None:
                return CapabilityDispatch("warehouse.shift_summary", "Текущая складская смена ещё не открыта.")
            rows = self.operations.list_operations(int(shift["id"]), actor_user_id=actor_user_id)
            return CapabilityDispatch("warehouse.shift_summary", f"В текущей складской смене записей: {len(rows)}.")
        if _REQUIRED_STATUS.search(cleaned):
            self._require(actor_user_id, "warehouse.required_status")
            if self.closing is None:
                raise RuntimeError("Required writeoffs unavailable")
            overview = self.closing.overview(actor_user_id=actor_user_id)
            return CapabilityDispatch(
                "warehouse.required_status",
                "Обязательные списания: "
                f"{overview['requirements_resolved']} из {overview['requirements_total']} выполнено.",
            )
        return None

    def execute(
        self,
        row: dict[str, Any],
        actor_user_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        capability = str(row["capability_id"])
        self._require(actor_user_id, capability)
        arguments = dict(row["arguments"])
        if capability == "attendance.open_own":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            session, created = self.attendance.open(actor_user_id, connection)
            return {"status": "opened" if created else "already_open", "revision": int(session["revision"])}
        if capability == "attendance.close_own":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            active = self.attendance.status(actor_user_id, connection)
            if active is None or str(active["id"]) != str(row.get("target_id")) or int(active["revision"]) != int(row.get("target_revision")):
                raise AssistantProposalTargetConflict("Attendance changed")
            session, closed = self.attendance.close(actor_user_id, connection)
            return {"status": "closed" if closed else "already_closed", "revision": int(session["revision"]) if session else 0}
        if capability == "attendance.force_close":
            if self.attendance is None:
                raise RuntimeError("Attendance unavailable")
            result = self.attendance.force_close(
                int(arguments["target_ref"]), actor_user_id,
                str(arguments["reason"]), int(arguments["expected_revision"]),
                connection,
            )
            return {"status": "force_closed", "revision": int(result["revision"])}
        if capability == "reminders.update":
            if self.reminders is None:
                raise RuntimeError("Reminder settings unavailable")
            result = self.reminders.update(
                int(arguments["target_ref"]), dict(arguments["values"]),
                int(row["target_revision"]), actor_user_id,
                connection,
            )
            return {"status": "updated", "revision": int(result["revision"])}
        raise ValueError("Capability executor is not allowlisted")
