"""Bounded AI assistant services."""

from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.context import (
    AssistantConversationContext,
    AssistantConversationMemory,
    AssistantConversationTurn,
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

__all__ = [
    "AssistantOrchestrator",
    "AssistantRequestCoordinator",
    "AssistantConversationContext",
    "AssistantConversationMemory",
    "AssistantConversationTurn",
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
]
