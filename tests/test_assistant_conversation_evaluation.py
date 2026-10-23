from pathlib import Path

import pytest

from app.services.assistant.evaluation import (
    ConversationEvaluationRunner,
    EvaluationExpectation,
    EvaluationTurn,
    ScriptedEvaluationAdapter,
)


def test_scripted_runner_scores_and_writes_only_explicit_output(tmp_path: Path) -> None:
    turns = tuple(
        EvaluationTurn("d1", f"t{index}", "synthetic", EvaluationExpectation(
            dialogue_act="new_request", domain="catalog", capability_id="catalog.get"
        )) for index in range(3)
    )
    runner = ConversationEvaluationRunner(corpus_version="v1")
    report = runner.run(turns, ScriptedEvaluationAdapter())
    assert report["provider_mode"] == "offline_scripted"
    assert report["metrics"] == {name: 1.0 for name in (
        "route", "entity", "reference", "clarification", "argument", "memory", "safety"
    )}
    assert list(tmp_path.iterdir()) == []
    output = tmp_path / "report.json"
    runner.run(turns, ScriptedEvaluationAdapter(), output_path=output)
    assert output.stat().st_size < 1_000_000


def test_live_mode_requires_explicit_flag() -> None:
    class Live(ScriptedEvaluationAdapter):
        mode = "live"
    turn = EvaluationTurn("d", "t", "synthetic", EvaluationExpectation(
        dialogue_act="general_chat", domain="general", capability_id=None
    ))
    with pytest.raises(PermissionError):
        ConversationEvaluationRunner(corpus_version="v1").run((turn,), Live())
