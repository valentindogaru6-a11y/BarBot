"""Run the frozen offline-scripted assistant corpus without network access."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.assistant.capabilities import CAPABILITIES
from app.services.assistant.evaluation import (
    ConversationEvaluationRunner,
    EvaluationExpectation,
    EvaluationTurn,
    ScriptedEvaluationAdapter,
)
from tests.fixtures.assistant_conversation_corpus_v1 import CORPUS, CORPUS_VERSION


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    domains = {item.id: item.category for item in CAPABILITIES}
    turns = tuple(
        EvaluationTurn(
            dialogue_id=dialogue.identifier,
            turn_id=f"{dialogue.identifier}-t{index}",
            text=turn.text,
            expected=EvaluationExpectation(
                dialogue_act=("cancellation" if turn.expected_capability is None else "new_request"),
                domain=domains.get(turn.expected_capability, "general"),
                capability_id=turn.expected_capability,
                safety_ok=not turn.confirmation,
            ),
        )
        for dialogue in CORPUS
        for index, turn in enumerate(dialogue.turns, 1)
    )
    report = ConversationEvaluationRunner(
        corpus_version=CORPUS_VERSION, allow_live=args.allow_live
    ).run(turns, ScriptedEvaluationAdapter(), output_path=args.output)
    print(
        f"mode={report['provider_mode']} dialogues={report['dialogue_count']} "
        f"turns={report['turn_count']} contract_pass={report['contract_pass']}"
    )
    print("metrics=" + ",".join(
        f"{name}:{value:.3f}"
        for name, value in sorted(report["metrics"].items())
    ))
    return 0 if report["contract_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
