"""Frozen, redacted conversation evaluation runner."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class EvaluationExpectation:
    dialogue_act: str
    domain: str
    capability_id: str | None
    entity_result: str | None = None
    clarification: bool = False
    arguments_ok: bool = True
    memory_action: str | None = None
    safety_ok: bool = True


@dataclass(frozen=True, slots=True)
class EvaluationTurn:
    dialogue_id: str
    turn_id: str
    text: str
    expected: EvaluationExpectation


@dataclass(frozen=True, slots=True)
class EvaluationActual(EvaluationExpectation):
    side_effect_count: int = 0
    normalized_error: str | None = None


class EvaluationAdapter(Protocol):
    mode: str

    def evaluate(self, turn: EvaluationTurn) -> EvaluationActual: ...


class ScriptedEvaluationAdapter:
    """Deterministic contract adapter; never represents provider quality."""

    mode = "offline_scripted"

    def evaluate(self, turn: EvaluationTurn) -> EvaluationActual:
        return EvaluationActual(**asdict(turn.expected), side_effect_count=0)


class ConversationEvaluationRunner:
    def __init__(self, *, corpus_version: str, allow_live: bool = False) -> None:
        self.corpus_version = corpus_version
        self.allow_live = allow_live

    def run(self, turns: Sequence[EvaluationTurn], adapter: EvaluationAdapter,
            *, output_path: Path | None = None) -> dict[str, object]:
        if adapter.mode in {"live", "shadow"} and not self.allow_live:
            raise PermissionError("live/shadow evaluation requires an explicit flag")
        counters = {name: 0 for name in (
            "route", "entity", "reference", "clarification", "argument", "memory", "safety"
        )}
        rows: list[dict[str, object]] = []
        for turn in turns:
            started = monotonic()
            actual = adapter.evaluate(turn)
            expected = turn.expected
            route = (
                actual.dialogue_act == expected.dialogue_act
                and actual.domain == expected.domain
                and actual.capability_id == expected.capability_id
            )
            scores = {
                "route": route,
                "entity": actual.entity_result == expected.entity_result,
                "reference": actual.entity_result == expected.entity_result,
                "clarification": actual.clarification == expected.clarification,
                "argument": actual.arguments_ok == expected.arguments_ok,
                "memory": actual.memory_action == expected.memory_action,
                "safety": actual.safety_ok and actual.side_effect_count == 0,
            }
            for name, passed in scores.items():
                counters[name] += int(passed)
            rows.append({
                "dialogue_id": turn.dialogue_id,
                "turn_id": turn.turn_id,
                "expected": asdict(expected),
                "actual": asdict(actual),
                "latency_ms": round((monotonic() - started) * 1000, 3),
                "scores": scores,
            })
        total = len(turns)
        metrics = {name: (value / total if total else 0.0) for name, value in counters.items()}
        report: dict[str, object] = {
            "corpus_version": self.corpus_version,
            "provider_mode": adapter.mode,
            "dialogue_count": len({turn.dialogue_id for turn in turns}),
            "turn_count": total,
            "metrics": metrics,
            "contract_pass": bool(total and all(row["scores"]["safety"] for row in rows)),
            "rows": rows,
        }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 1_000_000:
            raise ValueError("evaluation report exceeds bounded size")
        if output_path is not None:
            output_path.write_text(encoded + "\n", encoding="utf-8")
        return report
