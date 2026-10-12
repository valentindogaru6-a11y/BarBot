"""Authoritative role- and flag-aware inventory of BarBot capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel

from app.models.assistant_capabilities import (
    CapabilityResult,
    CapabilityRisk,
    ConfirmationPolicy,
    EntityCapabilityInput,
    MutationCapabilityInput,
    NoCapabilityInput,
    PeriodCapabilityInput,
    QueryCapabilityInput,
    WarehouseCapabilityInput,
)
from app.models.user import Role


ModelType: TypeAlias = type[BaseModel]
_ALL_ACTIVE = frozenset({Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER, Role.ACCOUNTANT, Role.USER})
_WAREHOUSE_VIEW = frozenset({Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER, Role.ACCOUNTANT})
_WAREHOUSE_WRITE = frozenset({Role.SUPER_ADMIN, Role.ADMIN, Role.BARTENDER})
_ADMIN = frozenset({Role.SUPER_ADMIN, Role.ADMIN})


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    id: str
    title: str
    description: str
    category: str
    allowed_roles: frozenset[Role]
    chat_policy: str
    feature_flags: tuple[str, ...]
    risk_level: CapabilityRisk
    input_model: ModelType
    result_model: ModelType
    confirmation_policy: ConfirmationPolicy
    adapter: str
    availability: str


@dataclass(frozen=True, slots=True)
class UiCapabilityCoverage:
    ui_action: str
    capability_id: str | None
    exclusion: str | None = None


def _cap(
    identifier: str,
    title: str,
    category: str,
    roles: frozenset[Role],
    risk: CapabilityRisk,
    adapter: str,
    *,
    description: str = "",
    input_model: ModelType = NoCapabilityInput,
    confirmation: ConfirmationPolicy | None = None,
    flags: tuple[str, ...] = ("ai_assistant_enabled", "warehouse_enabled"),
    availability: str = "service",
) -> CapabilityDefinition:
    if confirmation is None:
        confirmation = (
            ConfirmationPolicy.NONE
            if risk <= CapabilityRisk.FLOW
            else ConfirmationPolicy.EXPLICIT
        )
    return CapabilityDefinition(
        id=identifier,
        title=title,
        description=description or title,
        category=category,
        allowed_roles=roles,
        chat_policy="private",
        feature_flags=flags,
        risk_level=risk,
        input_model=input_model,
        result_model=CapabilityResult,
        confirmation_policy=confirmation,
        adapter=adapter,
        availability=availability,
    )


CAPABILITIES = (
    _cap("navigation.main", "Открыть главное меню", "navigation", _ALL_ACTIVE, CapabilityRisk.READ, "open_main", flags=("ai_assistant_enabled",)),
    _cap("catalog.search", "Найти техкарту", "catalog", _ALL_ACTIVE, CapabilityRisk.READ, "search_tech_cards", input_model=QueryCapabilityInput, flags=("ai_assistant_enabled",)),
    _cap("catalog.get", "Открыть техкарту", "catalog", _ALL_ACTIVE, CapabilityRisk.READ, "get_tech_card", input_model=EntityCapabilityInput, flags=("ai_assistant_enabled",)),
    _cap("catalog.manage", "Управлять каталогом", "catalog", _ADMIN, CapabilityRisk.MUTATION, "catalog_manage", input_model=MutationCapabilityInput, flags=("ai_assistant_enabled",)),
    _cap("users.list", "Показать сотрудников", "employees", _ADMIN, CapabilityRisk.READ, "list_registered_users"),
    _cap("users.list_bartenders", "Показать барменов", "employees", _ADMIN, CapabilityRisk.READ, "list_registered_bartenders"),
    _cap("users.get", "Показать сотрудника", "employees", _ADMIN, CapabilityRisk.READ, "get_user_summary", input_model=QueryCapabilityInput),
    _cap("users.change_role", "Изменить роль сотрудника", "employees", _ADMIN, CapabilityRisk.HIGH, "change_user_role", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("attendance.status", "Показать мою смену", "attendance", _WAREHOUSE_WRITE, CapabilityRisk.READ, "attendance_status"),
    _cap("attendance.list_open", "Показать открытые смены", "attendance", _ADMIN, CapabilityRisk.READ, "list_open_attendance_sessions"),
    _cap("attendance.get", "Показать персональную смену", "attendance", _ADMIN, CapabilityRisk.READ, "get_attendance_session", input_model=EntityCapabilityInput),
    _cap("attendance.open_own", "Открыть мою смену", "attendance", _WAREHOUSE_WRITE, CapabilityRisk.MUTATION, "attendance_open_own"),
    _cap("attendance.close_own", "Закрыть мою смену", "attendance", _WAREHOUSE_WRITE, CapabilityRisk.MUTATION, "attendance_close_own"),
    _cap("attendance.force_close", "Принудительно закрыть чужую смену", "attendance", _ADMIN, CapabilityRisk.HIGH, "attendance_force_close", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("attendance_reports.generate", "Сформировать табель XLSX", "reports", _ADMIN | frozenset({Role.ACCOUNTANT}), CapabilityRisk.FLOW, "attendance_xlsx", input_model=PeriodCapabilityInput),
    _cap("warehouse.shift_summary", "Показать текущую складскую смену", "warehouse", _WAREHOUSE_VIEW, CapabilityRisk.READ, "get_current_shift_summary"),
    _cap("warehouse.list_operations", "Показать записи смены", "warehouse", _WAREHOUSE_VIEW, CapabilityRisk.READ, "list_shift_operations"),
    _cap("warehouse.required_status", "Показать обязательные списания", "warehouse", _WAREHOUSE_VIEW, CapabilityRisk.READ, "get_required_writeoff_status"),
    _cap("warehouse.prepare_writeoff", "Подготовить списание", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.FLOW, "prepare_writeoff", input_model=WarehouseCapabilityInput),
    _cap("warehouse.prepare_transfer", "Подготовить перемещение", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.FLOW, "prepare_transfer", input_model=WarehouseCapabilityInput),
    _cap("warehouse.commit_batch", "Подтвердить складскую операцию", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.MUTATION, "commit_batch", input_model=EntityCapabilityInput),
    _cap("warehouse.cancel_batch", "Отменить складской draft", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.MUTATION, "cancel_batch", input_model=EntityCapabilityInput),
    _cap("warehouse.start_required_flow", "Запустить обязательные списания", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.FLOW, "start_required_writeoff_flow"),
    _cap("warehouse.update_operation", "Исправить складскую операцию", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.HIGH, "update_operation", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("warehouse.delete_operation", "Удалить складскую операцию", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.HIGH, "delete_operation", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("warehouse.close_shift", "Закрыть общую складскую смену", "warehouse", _WAREHOUSE_WRITE, CapabilityRisk.HIGH, "close_shift", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("warehouse.open_correction", "Открыть корректировку", "warehouse", _ADMIN, CapabilityRisk.HIGH, "open_correction", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("warehouse.commit_correction", "Выпустить корректировку", "warehouse", _ADMIN, CapabilityRisk.HIGH, "commit_correction", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.REASON_AND_EXPLICIT),
    _cap("reports.generate", "Сформировать складской отчёт", "reports", _WAREHOUSE_WRITE, CapabilityRisk.FLOW, "generate_report", input_model=PeriodCapabilityInput),
    _cap("reports.status", "Показать статус отчёта", "reports", _WAREHOUSE_VIEW, CapabilityRisk.READ, "get_report_status", input_model=EntityCapabilityInput),
    _cap("reports.history", "Показать историю отчётов", "reports", _WAREHOUSE_VIEW, CapabilityRisk.READ, "list_report_history"),
    _cap("reports.send", "Отправить отчёт", "reports", _WAREHOUSE_WRITE, CapabilityRisk.HIGH, "send_report", input_model=MutationCapabilityInput, confirmation=ConfirmationPolicy.EXPLICIT),
    _cap("recipients.list", "Показать получателей отчётов", "recipients", _ADMIN, CapabilityRisk.READ, "list_report_recipients"),
    _cap("recipients.create_or_update", "Настроить получателя", "recipients", _ADMIN, CapabilityRisk.MUTATION, "upsert_recipient", input_model=MutationCapabilityInput),
    _cap("recipients.disable", "Отключить получателя", "recipients", _ADMIN, CapabilityRisk.MUTATION, "disable_recipient", input_model=MutationCapabilityInput),
    _cap("reminders.get", "Показать настройки напоминаний", "reminders", _ADMIN, CapabilityRisk.READ, "get_reminder_settings"),
    _cap("reminders.update", "Изменить напоминания", "reminders", _ADMIN, CapabilityRisk.MUTATION, "update_reminders", input_model=MutationCapabilityInput),
    _cap("schedules.list", "Показать расписания", "schedules", _ADMIN, CapabilityRisk.READ, "list_schedules"),
    _cap("schedules.create_or_update", "Настроить автоотправку", "schedules", _ADMIN, CapabilityRisk.MUTATION, "upsert_schedule", input_model=MutationCapabilityInput),
    _cap("schedules.disable", "Отключить автоотправку", "schedules", _ADMIN, CapabilityRisk.MUTATION, "disable_schedule", input_model=MutationCapabilityInput),
    _cap("capabilities.list", "Показать доступные возможности", "help", _ALL_ACTIVE, CapabilityRisk.READ, "get_capabilities", input_model=QueryCapabilityInput, flags=("ai_assistant_enabled",), availability="registry"),
)


UI_CAPABILITY_COVERAGE = (
    UiCapabilityCoverage("main.catalog", "catalog.search"),
    UiCapabilityCoverage("main.search", "catalog.search"),
    UiCapabilityCoverage("main.attendance", "attendance.status"),
    UiCapabilityCoverage("warehouse.operations", "warehouse.list_operations"),
    UiCapabilityCoverage("warehouse.writeoff", "warehouse.prepare_writeoff"),
    UiCapabilityCoverage("warehouse.transfer", "warehouse.prepare_transfer"),
    UiCapabilityCoverage("warehouse.required", "warehouse.start_required_flow"),
    UiCapabilityCoverage("warehouse.close", "warehouse.close_shift"),
    UiCapabilityCoverage("warehouse.reports", "reports.history"),
    UiCapabilityCoverage("warehouse.recipients", "recipients.list"),
    UiCapabilityCoverage("warehouse.reminders", "reminders.get"),
    UiCapabilityCoverage("warehouse.schedules", "schedules.list"),
    UiCapabilityCoverage("warehouse.corrections", "warehouse.open_correction"),
    UiCapabilityCoverage("attendance.open", "attendance.open_own"),
    UiCapabilityCoverage("attendance.close", "attendance.close_own"),
    UiCapabilityCoverage("attendance.force_close", "attendance.force_close"),
    UiCapabilityCoverage("attendance.xlsx", "attendance_reports.generate"),
    UiCapabilityCoverage("admin.catalog", "catalog.manage"),
    UiCapabilityCoverage("admin.users", "users.list"),
    UiCapabilityCoverage("navigation.back", None, "pure navigation"),
    UiCapabilityCoverage("pagination", None, "pure navigation"),
    UiCapabilityCoverage("system.deployment", None, "outside Telegram BarBot"),
)


class CapabilityRegistry:
    def __init__(self, capabilities: tuple[CapabilityDefinition, ...] = CAPABILITIES) -> None:
        by_id = {capability.id: capability for capability in capabilities}
        if len(by_id) != len(capabilities):
            raise ValueError("Duplicate capability id")
        self._capabilities = capabilities
        self._by_id = by_id

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        return self._by_id.get(capability_id)

    def require(self, capability_id: str) -> CapabilityDefinition:
        capability = self.get(capability_id)
        if capability is None:
            raise ValueError("Unknown capability")
        return capability

    def available(
        self,
        *,
        role: Role,
        chat_type: str = "private",
        flags: dict[str, bool] | None = None,
        adapters: frozenset[str] | None = None,
    ) -> tuple[CapabilityDefinition, ...]:
        active_flags = flags or {}
        return tuple(
            capability
            for capability in self._capabilities
            if role in capability.allowed_roles
            and capability.chat_policy == chat_type
            and all(active_flags.get(flag, True) for flag in capability.feature_flags)
            and (adapters is None or capability.availability == "registry" or capability.adapter in adapters)
        )

    def describe(self, **filters: object) -> dict[str, tuple[CapabilityDefinition, ...]]:
        grouped: dict[str, list[CapabilityDefinition]] = {}
        for capability in self.available(**filters):  # type: ignore[arg-type]
            grouped.setdefault(capability.category, []).append(capability)
        return {
            category: tuple(sorted(values, key=lambda value: value.title.casefold()))
            for category, values in sorted(grouped.items())
        }

    @property
    def all(self) -> tuple[CapabilityDefinition, ...]:
        return self._capabilities
