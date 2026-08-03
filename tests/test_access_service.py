"""Tests for centralized role resolution and permissions."""

from pathlib import Path

import pytest

from app.models.user import Role
from app.services.access_service import AccessDeniedError
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.storage.user_database import UserDatabase


def _services(tmp_path: Path) -> tuple[AuthService, UserService, Path]:
    database = UserDatabase(tmp_path / "barbot.db")
    database.initialize()
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditService(audit_path)
    users = UserService(database, audit)
    access = AuthService(frozenset({1, 2}), users, audit)
    for user_id in (1, 2, 10, 20, 30, 40):
        users.ensure_user(user_id)
    database.set_role(10, Role.ADMIN, actor_user_id=1)
    database.set_role(20, Role.BARTENDER, actor_user_id=10)
    database.set_role(30, Role.USER, actor_user_id=10)
    database.set_role(40, Role.BLOCKED, actor_user_id=10)
    return access, users, audit_path


def test_resolves_all_roles_and_catalog_access(tmp_path: Path) -> None:
    access, _, _ = _services(tmp_path)

    assert access.get_context(1).role is Role.SUPER_ADMIN
    assert access.get_context(10).role is Role.ADMIN
    assert access.get_context(20).role is Role.BARTENDER
    assert access.get_context(30).role is Role.USER
    assert access.get_context(40).role is Role.BLOCKED
    assert access.can_manage_catalog(1)
    assert access.can_manage_catalog(10)
    assert not access.can_manage_catalog(20)
    assert not access.can_view_catalog(40)


def test_super_admin_assigns_admin_and_admin_assigns_bartender(
    tmp_path: Path,
) -> None:
    access, _, _ = _services(tmp_path)

    assert access.change_role(1, 30, Role.ADMIN).role is Role.ADMIN
    assert access.change_role(10, 20, Role.USER).role is Role.USER
    assert access.change_role(10, 20, Role.BARTENDER).role is Role.BARTENDER


def test_admin_cannot_assign_admin_or_change_admin(tmp_path: Path) -> None:
    access, _, audit_path = _services(tmp_path)

    with pytest.raises(AccessDeniedError, match="Недостаточно прав"):
        access.change_role(10, 30, Role.ADMIN)
    with pytest.raises(AccessDeniedError):
        access.change_role(10, 1, Role.USER)

    assert "role_change_denied" in audit_path.read_text(encoding="utf-8")


def test_super_admin_cannot_be_changed_or_change_self(tmp_path: Path) -> None:
    access, _, _ = _services(tmp_path)

    assert not access.can_change_role(1, 1, Role.USER)
    assert not access.can_change_role(1, 2, Role.BLOCKED)
    with pytest.raises(AccessDeniedError):
        access.change_role(1, 2, Role.USER)


def test_admin_cannot_change_own_role(tmp_path: Path) -> None:
    access, _, _ = _services(tmp_path)

    assert not access.can_change_role(10, 10, Role.USER)
    assert access.available_role_changes(10, 10) == []
    assert access.available_role_changes(10, 1) == []


def test_block_and_unblock_are_audited(tmp_path: Path) -> None:
    access, _, audit_path = _services(tmp_path)

    access.change_role(10, 30, Role.BLOCKED)
    access.change_role(10, 30, Role.USER)
    audit = audit_path.read_text(encoding="utf-8")

    assert "user_blocked" in audit
    assert "user_unblocked" in audit
