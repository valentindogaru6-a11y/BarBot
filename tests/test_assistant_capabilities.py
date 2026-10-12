from app.models.assistant_capabilities import CapabilityRisk, ConfirmationPolicy
from app.models.user import Role
from app.services.assistant.capabilities import (
    CapabilityRegistry,
    UI_CAPABILITY_COVERAGE,
)


def test_registry_ids_adapters_and_ui_coverage_are_explicit() -> None:
    registry = CapabilityRegistry()
    assert len({item.id for item in registry.all}) == len(registry.all)
    assert all(item.adapter and item.description for item in registry.all)
    known = {item.id for item in registry.all}
    assert all(
        row.capability_id in known or row.exclusion
        for row in UI_CAPABILITY_COVERAGE
    )
    assert all(not (row.capability_id and row.exclusion) for row in UI_CAPABILITY_COVERAGE)


def test_registry_filters_roles_flags_and_private_chat() -> None:
    registry = CapabilityRegistry()
    flags = {"ai_assistant_enabled": True, "warehouse_enabled": True}
    bartender = {item.id for item in registry.available(role=Role.BARTENDER, flags=flags)}
    accountant = {item.id for item in registry.available(role=Role.ACCOUNTANT, flags=flags)}
    admin = {item.id for item in registry.available(role=Role.ADMIN, flags=flags)}
    assert "warehouse.prepare_writeoff" in bartender
    assert "users.change_role" not in bartender
    assert "reports.history" in accountant
    assert "warehouse.prepare_writeoff" not in accountant
    assert "attendance.force_close" in admin
    assert registry.available(role=Role.BLOCKED, flags=flags) == ()
    assert registry.available(role=Role.ADMIN, chat_type="group", flags=flags) == ()
    assert registry.available(
        role=Role.ADMIN,
        flags={"ai_assistant_enabled": True, "warehouse_enabled": False},
    ) == tuple(
        item
        for item in registry.available(role=Role.ADMIN, flags=flags)
        if "warehouse_enabled" not in item.feature_flags
    )


def test_all_mutations_require_confirmation_and_high_risk_requires_reason() -> None:
    for capability in CapabilityRegistry().all:
        if capability.risk_level >= CapabilityRisk.MUTATION:
            assert capability.confirmation_policy is not ConfirmationPolicy.NONE
        if capability.risk_level is CapabilityRisk.HIGH:
            assert capability.confirmation_policy in {
                ConfirmationPolicy.EXPLICIT,
                ConfirmationPolicy.REASON_AND_EXPLICIT,
            }
