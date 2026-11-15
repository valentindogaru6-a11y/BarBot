"""Closed bounded JSON IPC for fixed read-only executors. No pickle or code RPC."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.models.assistant import AssistantIntent, AssistantIntentName
from app.models.catalog import Catalog
from app.models.user import Role
from app.services.assistant.capabilities import CAPABILITIES

MAX_REQUEST = 2_000_000
MAX_RESPONSE = 128_000
VERSION = 1


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


def encode(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def bounded(value, depth=0):
    if depth > 16:
        raise ValueError('read_depth')
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError('read_items')
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 100:
                raise ValueError('read_key')
            bounded(child, depth+1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise ValueError('read_items')
        for child in value:
            bounded(child, depth+1)
    elif isinstance(value, str):
        if len(value) > 12000 or '\0' in value:
            raise ValueError('read_string')
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError('read_type')


@lru_cache(maxsize=1)
def manifest():
    rows = [dict(id=c.id, risk=int(c.risk_level), roles=sorted(r.value for r in c.allowed_roles),
        flags=c.feature_flags, access=c.access_rule, adapter=c.adapter,
        arguments=c.input_model.model_json_schema(), result=c.result_model.model_json_schema())
        for c in CAPABILITIES]
    names = ('read_kernel.py', 'read_protocol.py', 'read_broker.py', 'read_worker.py', 'read_snapshot.py',
             'read_journal.py','read_lifecycle.py','read_process.py',
             'read_only.py', 'read_results.py', 'tools.py', 'orchestrator.py', 'capability_controller.py', 'retrieval.py')
    hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}
    return hashlib.sha256(encode(dict(version=VERSION, rows=rows, code=hashes))).hexdigest()


class AccessFacts(Closed):
    actor_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    role: Role
    active: bool
    private: bool
    eligible: bool
    mode: Literal['pilot_allowlist', 'role_based']
    pilot_allowed: bool
    ai_enabled: bool
    warehouse_enabled: bool
    available: tuple[str, ...] = Field(max_length=64)
    bound: tuple[str, ...] = Field(max_length=64)


class ReadRequest(Closed):
    version: Literal[1] = VERSION
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    capability_id: str = Field(min_length=1, max_length=100)
    mode: Literal['dispatch', 'tool', 'catalog']
    database: str | None = Field(default=None, max_length=4096)
    database_identity: tuple[int, int] | None = None
    access: AccessFacts | None = None
    # Trusted loaded catalog DTO, never a user/provider filesystem path.
    catalog: Catalog | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=10)
    catalog_intent: AssistantIntent | None = None
    candidate_ids: tuple[str, ...] = Field(default=(), max_length=5)
    deadline: float

    @model_validator(mode='after')
    def closed_execution(self):
        definition = next((c for c in CAPABILITIES if c.id == self.capability_id), None)
        if self.manifest_hash != manifest() or definition is None or int(definition.risk_level) != 0:
            raise ValueError('read_manifest')
        if self.mode == 'catalog':
            intent = self.catalog_intent
            if self.capability_id not in {'catalog.search', 'catalog.get'} or intent is None or self.catalog is None:
                raise ValueError('read_catalog')
            if (intent.intent is not AssistantIntentName.FIND_TECH_CARD or intent.items or intent.answer_text
                    or intent.clarification or intent.needs_web or intent.capability_id or self.arguments):
                raise ValueError('read_catalog_shape')
            if self.access is not None and (self.database is None or self.database_identity is None):
                raise ValueError('read_catalog_authority')
        else:
            if self.database is None or self.database_identity is None or self.access is None or self.catalog_intent:
                raise ValueError('read_authority')
            definition.input_model.model_validate(self.arguments)
        bounded(self.arguments)
        return self


class ReadResponse(Closed):
    version: Literal[1] = VERSION
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    capability_id: str = Field(min_length=1, max_length=100)
    status: Literal['ok', 'safety', 'infrastructure']
    category: Literal['ok', 'access', 'contract', 'read_only', 'state_changed', 'integrity', 'executor', 'serialization', 'deadline']
    result: JsonValue = None
    business_writes: int = Field(default=0, ge=0)
    authorizer_denials: int = Field(default=0, ge=0)
    business_before: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    business_after: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    integrity: bool = True
    startup_ms: float = Field(ge=0)
    execution_ms: float = Field(ge=0)
    rss_kib: int = Field(ge=0)

    @model_validator(mode='after')
    def materialized(self):
        bounded(self.result)
        if self.status != 'ok' and self.result is not None:
            raise ValueError('read_error_result')
        return self
