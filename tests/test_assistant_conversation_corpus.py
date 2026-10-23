from tests.fixtures.assistant_conversation_corpus_v1 import CORPUS, CORPUS_VERSION
from app.services.assistant.capabilities import CAPABILITIES


def test_v1_corpus_has_required_size_and_capability_coverage() -> None:
    assert CORPUS_VERSION == "bar-jarvis-conversation-v1"
    assert len(CORPUS) >= 60
    assert sum(len(dialogue.turns) for dialogue in CORPUS) >= 180
    covered = {
        turn.expected_capability
        for dialogue in CORPUS
        for turn in dialogue.turns
        if turn.expected_capability
    }
    assert covered == {item.id for item in CAPABILITIES}


def test_v1_corpus_mutations_never_expect_implicit_confirmation() -> None:
    mutation_turns = [
        turn for dialogue in CORPUS for turn in dialogue.turns if turn.mutation
    ]
    assert mutation_turns
    assert all(not turn.confirmation for turn in mutation_turns)
