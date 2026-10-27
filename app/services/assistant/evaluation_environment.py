"""Fresh synthetic service composition, confined to a newly owned temporary root."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.models.catalog import Catalog
from app.models.user import Role
from app.models.warehouse.nomenclature import BaseUnit, ProductOperationType
from app.services.admin_service import AdminService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.storage.user_database import UserDatabase
from app.storage.json_storage import JsonCatalogStorage
from app.storage.warehouse_database import WarehouseDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.assistant_conversation_database import AssistantConversationDatabase
from app.storage.assistant_memory_database import AssistantMemoryDatabase
from app.storage.assistant_action_database import AssistantActionDatabase
from app.services.assistant.context import PersistentAssistantConversationMemory
from app.services.assistant.long_term_memory import AssistantLongTermMemoryService
from app.services.assistant.capabilities import CapabilityBinding, CapabilityRegistry
from app.services.assistant.access_policy import AssistantAccessPolicy
from app.services.assistant.capability_controller import AssistantCapabilityController
from app.services.assistant.proposals import AssistantActionProposalService
from app.services.assistant.orchestrator import AssistantOrchestrator
from app.services.assistant.evaluation_budget import EvaluationSafetyError


class IsolatedEvaluationEnvironment:
    owner = 1
    chat = 1
    employee = 2
    telegram_transport_disabled = True

    def __enter__(self):
        self._temporary = TemporaryDirectory(prefix='barbot-live-eval-')
        self.root = Path(self._temporary.name)
        try:
            self.path = self.validate_path(self.root/'evaluation.db', must_be_new=True)
            UserDatabase(self.path).initialize()
            self.storage = JsonCatalogStorage(self.root/'catalog.json', self.root/'catalog-backups')
            self.storage.save(Catalog.model_validate({'version':1, 'categories':[{
                'id':'preparations','name':'Заготовки','items':[
                    {'id':key,'name':name,'ingredients':[{'name':'Вода','amount':'100','unit':'мл'}]}
                    for key,name in (('sea','Облепиха'),('berry','Ягодное пюре'),('tea','Ягодная основа для чая'))
                ]}]}))
            self.recreate()
            self.users.ensure_user(self.owner, first_name='Synthetic owner')
            self.users.ensure_user(self.employee, first_name='Дмитро')
            self.users.set_role(self.employee, Role.BARTENDER, actor_user_id=self.owner)
            nomenclature = WarehouseDatabase(self.path)
            if not any(location.normalized_name == 'кухня' for location in nomenclature.list_locations()):
                nomenclature.create_location(name='Кухня', normalized_name='кухня', is_active=True,
                    can_be_source=False, can_be_destination=True, actor_user_id=self.owner,
                    stable_id_factory=lambda _: 'kitchen')
            for key,name,unit in (('orange','Апельсин','kg'),('coconut','Кокосовое молоко','l')):
                nomenclature.create_product(official_name=name,normalized_name=name.casefold(),
                    base_unit=BaseUnit(unit),aliases=[],operation_types=set(ProductOperationType),
                    is_active=True,actor_user_id=self.owner,stable_id_factory=lambda _, key=key:key)
            self.attendance.open(self.employee)
            self.catalog_digest = self.digest()
            if self.state()['schema'] != 15:
                raise EvaluationSafetyError('invalid_temporary_schema')
            return self
        except BaseException:
            self._temporary.cleanup()
            raise

    def validate_path(self, path: Path, *, must_be_new=False) -> Path:
        path = Path(path)
        root = self.root.resolve(strict=True)
        if self.root.is_symlink() or not path.is_absolute() or not path.is_relative_to(root):
            raise EvaluationSafetyError('temporary_db_path_denied')
        relative = path.relative_to(root)
        current = root
        for component in relative.parts:
            current /= component
            if component in ('.','..') or current.is_symlink():
                raise EvaluationSafetyError('temporary_db_path_denied')
        if path.resolve().parent != root or path.suffix != '.db' or (must_be_new and path.exists()):
            raise EvaluationSafetyError('temporary_db_path_denied')
        return path

    def recreate(self):
        """Repositories are connection-per-call; no live SQLite handles survive."""
        self.validate_path(self.path)
        settings = Settings(_env_file=None,bot_token='synthetic',warehouse_enabled=True,
            ai_assistant_enabled=True,ai_access_mode='pilot_allowlist',ai_pilot_user_ids='1',
            groq_api_key=None,gemini_api_key=None)
        self.users = UserService(UserDatabase(self.path))
        self.access = AuthService(frozenset({self.owner}), self.users)
        self.attendance = WarehouseAttendanceService(WarehouseAttendanceDatabase(self.path),self.access)
        self.operations = WarehouseOperationsService(WarehouseOperationsDatabase(self.path),self.access)
        self.memory = PersistentAssistantConversationMemory(AssistantConversationDatabase(self.path))
        self.long_memory = AssistantLongTermMemoryService(AssistantMemoryDatabase(self.path))
        self.registry = CapabilityRegistry()
        self.policy = AssistantAccessPolicy(self.access,self.registry,settings)
        self.proposals = AssistantActionProposalService(AssistantActionDatabase(self.path),self.access,self.registry,self.policy)
        self.admin = AdminService(self.storage,AuditService(self.root/'synthetic-audit.jsonl'))
        self.orchestrator = AssistantOrchestrator(None,planner_mode='ai_first')
        external = {
            identifier:CapabilityBinding(identifier,self.orchestrator,self.admin,'catalog retrieval','CapabilityDispatch')
            for identifier in ('catalog.search','catalog.get')
        }
        for identifier in ('warehouse.prepare_writeoff','warehouse.prepare_transfer'):
            external[identifier] = CapabilityBinding(identifier,self.orchestrator,self.operations,'warehouse intake','CapabilityDispatch')
        for identifier,executor in (('warehouse.commit_batch',self.operations.commit_batch),('warehouse.cancel_batch',self.operations.cancel_batch)):
            external[identifier] = CapabilityBinding(identifier,self.orchestrator,self.operations,'warehouse confirmation','CapabilityDispatch',executor)
        self.controller = AssistantCapabilityController(self.registry,self.proposals,self.access,self.policy,
            flags={'ai_assistant_enabled':True,'warehouse_enabled':True},users=self.users,
            attendance=self.attendance,operations=self.operations,admin=self.admin,external_bindings=external)

    def digest(self):
        if self.storage.path != self.root/'catalog.json' or self.storage.path.is_symlink():
            raise EvaluationSafetyError('temporary_catalog_path_denied')
        return hashlib.sha256(self.storage.path.read_bytes()).hexdigest()

    def state(self) -> dict:
        self.validate_path(self.path)
        self.digest()
        for repository in (self.operations.repository,self.attendance.repository,self.memory.database,
                           self.long_memory.database,self.proposals.repository,self.users.database):
            if self.validate_path(repository.path) != self.path:
                raise EvaluationSafetyError('temporary_db_path_denied')
        connection = sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            def count(table, where='1'):
                return connection.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0]
            return dict(schema=connection.execute('PRAGMA user_version').fetchone()[0],
                operations=count('warehouse_operations'),
                attendance_open=count('warehouse_shift_sessions',"status='open'"),
                memory_active=count('assistant_long_term_memories',"status='active'"),
                memory_pending=count('assistant_long_term_memories',"status='pending'"),
                proposals=count('assistant_action_proposals'),
                proposals_ready=count('assistant_action_proposals',"status='ready'"),
                proposals_executed=count('assistant_action_proposals',"status='executed'"))
        finally:
            connection.close()

    def __exit__(self, *args):
        self._temporary.cleanup()
