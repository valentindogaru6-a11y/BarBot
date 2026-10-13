import pytest

from app.models.assistant_capabilities import CapabilityRisk, ConfirmationPolicy
from app.models.user import Role
from app.services.assistant.capabilities import (
    CapabilityBinding,
    CapabilityRegistry,
    UI_CAPABILITY_COVERAGE,
)
from app.services.access_service import UserContext


class Access:
    def __init__(self, role: Role) -> None:
        self.role = role

    def get_context(self, actor: int) -> UserContext:
        return UserContext(actor, self.role, None)


def _bind_all(registry: CapabilityRegistry) -> None:
    adapter = object()
    executor = lambda: None
    registry.bind({
        item.id: CapabilityBinding(
            item.id,
            adapter,
            adapter,
            "test route",
            "test renderer",
            executor if item.risk_level >= CapabilityRisk.MUTATION else None,
        )
        for item in registry.all
    })


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


def test_runtime_bindings_and_access_service_are_the_discovery_source() -> None:
    registry = CapabilityRegistry()
    _bind_all(registry)
    flags = {"ai_assistant_enabled": True, "warehouse_enabled": True}
    available = {
        role: {
            item.id
            for item in registry.available_for_actor(
                Access(role), 1, flags=flags
            )
        }
        for role in Role
    }
    assert available[Role.USER] == set()
    assert available[Role.BLOCKED] == set()
    assert "catalog.search" not in available[Role.ACCOUNTANT]
    assert "warehouse.prepare_writeoff" not in available[Role.ACCOUNTANT]
    assert "reports.history" in available[Role.ACCOUNTANT]
    assert "users.change_role" not in available[Role.BARTENDER]
    assert "users.change_role" in available[Role.ADMIN]

    without_warehouse = {
        item.id
        for item in registry.available_for_actor(
            Access(Role.ADMIN),
            1,
            flags={"ai_assistant_enabled": True, "warehouse_enabled": False},
        )
    }
    assert {"users.list", "users.get", "users.change_role", "catalog.manage"} <= without_warehouse
    assert "warehouse.list_operations" not in without_warehouse


def test_ui_coverage_has_concrete_non_placeholder_runtime_bindings() -> None:
    registry = CapabilityRegistry()
    _bind_all(registry)
    for coverage in UI_CAPABILITY_COVERAGE:
        if coverage.capability_id is None:
            assert coverage.exclusion in {"pure navigation", "outside Telegram BarBot"}
            continue
        binding = registry.bindings[coverage.capability_id]
        assert binding.executable
        assert binding.route != "placeholder"
        assert binding.renderer != "placeholder"


def test_mutation_binding_requires_a_callable_executor() -> None:
    registry = CapabilityRegistry()
    marker = object()
    binding = CapabilityBinding(
        "attendance.open_own", marker, marker, "route", "renderer", marker
    )
    with pytest.raises(ValueError, match="has no executor"):
        registry.bind({binding.capability_id: binding})
