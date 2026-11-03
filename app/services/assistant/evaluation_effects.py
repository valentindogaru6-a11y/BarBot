"""Scorer-owned effect contracts. Never serialized into planner input."""

from dataclasses import dataclass
from enum import StrEnum


class EffectKind(StrEnum):
    READ_ONLY = 'read_only'
    CONTEXT_ONLY = 'context_only'
    PROPOSAL = 'proposal_expected'
    CONFIRM = 'confirm_expected'
    CANCEL = 'cancel_expected'
    MEMORY_PREVIEW = 'memory_preview_expected'
    MEMORY_CONFIRM = 'memory_confirm_expected'
    MEMORY_DELETE_PREVIEW = 'memory_delete_preview_expected'
    MEMORY_DELETE = 'memory_delete_expected'
    MEMORY_CANCEL = 'memory_cancel_expected'


@dataclass(frozen=True)
class EffectPolicy:
    kind: EffectKind = EffectKind.READ_ONLY
    target: str = ''
    maximum: int = 1


READ_ONLY = EffectPolicy()


def declared_policy(action, capabilities=(), *, confirm_target='', memory_save=False):
    """Definition-time defaults, independent of actual route, prose or result."""
    if action in ('begin_dialogue','cancel_task','cancel_proposal'):
        return EffectPolicy(EffectKind.CANCEL)
    if action == 'confirm_proposal':
        return EffectPolicy(EffectKind.CONFIRM,confirm_target)
    if action == 'confirm_memory':
        return EffectPolicy(EffectKind.MEMORY_CONFIRM)
    if action == 'delete_memory_preview':
        return EffectPolicy(EffectKind.MEMORY_DELETE_PREVIEW)
    if action == 'confirm_memory_delete':
        return EffectPolicy(EffectKind.MEMORY_DELETE)
    if action in ('cancel_memory','cancel_memory_delete'):
        return EffectPolicy(EffectKind.MEMORY_CANCEL)
    if action in ('restart','reset_context','restore_pilot','revoke_pilot'):
        return EffectPolicy(EffectKind.CONTEXT_ONLY)
    if memory_save:
        return EffectPolicy(EffectKind.MEMORY_PREVIEW)
    for capability in capabilities or ():
        if capability in ('warehouse.prepare_writeoff','warehouse.prepare_transfer','attendance.open_own',
                          'attendance.force_close','catalog.manage','reports.generate','reports.send',
                          'warehouse.start_required_flow','users.change_role','recipients.disable','reminders.update'):
            return EffectPolicy(EffectKind.PROPOSAL,capability)
    return READ_ONLY
