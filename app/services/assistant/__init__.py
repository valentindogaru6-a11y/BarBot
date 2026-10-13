"""Bounded AI assistant services."""

from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.context import (
    AssistantConversationContext,
    AssistantConversationMemory,
    AssistantConversationTurn,
    PersistentAssistantConversationMemory,
)
from app.services.assistant.orchestrator import (
    AssistantOrchestrator,
    AssistantToolAction,
    AssistantToolRegistry,
    AssistantUnavailableError,
)
from app.services.assistant.providers import (
    AioHttpTransport,
    AssistantHttpTransport,
    GeminiIntentClient,
    GeminiGroundedSearchClient,
    GroundedSearchClient,
    GroundedSearchResult,
    GroundedSource,
    GroqSpeechToTextClient,
    IntentModelClient,
    ProviderError,
    ProviderErrorKind,
    SpeechToTextClient,
)
from app.services.assistant.retrieval import (
    AssistantCatalogRetriever,
    CatalogRetrieval,
    RankedCatalogCandidate,
    candidate_key,
    display_catalog_entity,
    extract_catalog_entity,
    normalize_assistant_text,
)
from app.services.assistant.system_prompt import (
    ASSISTANT_SYSTEM_PROMPT_VERSION,
    build_assistant_system_prompt,
)
from app.services.assistant.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityRegistry,
    UI_CAPABILITY_COVERAGE,
)
from app.services.assistant.proposals import AssistantActionProposalService
from app.services.assistant.capability_controller import (
    AssistantCapabilityController,
    CapabilityDispatch,
)

__all__ = [
    "AssistantOrchestrator",
    "AssistantRequestCoordinator",
    "AssistantConversationContext",
    "AssistantConversationMemory",
    "AssistantConversationTurn",
    "PersistentAssistantConversationMemory",
    "AssistantCatalogRetriever",
    "AssistantToolAction",
    "AssistantToolRegistry",
    "AssistantUnavailableError",
    "AioHttpTransport",
    "AssistantHttpTransport",
    "GeminiIntentClient",
    "GeminiGroundedSearchClient",
    "GroundedSearchClient",
    "GroundedSearchResult",
    "GroundedSource",
    "GroqSpeechToTextClient",
    "IntentModelClient",
    "ProviderError",
    "ProviderErrorKind",
    "SpeechToTextClient",
    "CatalogRetrieval",
    "RankedCatalogCandidate",
    "candidate_key",
    "display_catalog_entity",
    "extract_catalog_entity",
    "normalize_assistant_text",
    "ASSISTANT_SYSTEM_PROMPT_VERSION",
    "build_assistant_system_prompt",
    "CapabilityDefinition",
    "CapabilityBinding",
    "CapabilityRegistry",
    "UI_CAPABILITY_COVERAGE",
    "AssistantActionProposalService",
    "AssistantCapabilityController",
    "CapabilityDispatch",
]
