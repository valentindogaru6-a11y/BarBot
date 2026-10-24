"""Frozen, redacted conversation evaluation runner."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol, Sequence

from app.services.assistant.capabilities import CAPABILITIES


REQUIRED_METRICS = (
    "route", "entity", "reference", "clarification", "argument", "memory", "safety"
)
SCRIPTED_THRESHOLDS = {name: 1.0 for name in REQUIRED_METRICS}


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    dialogue_count: int = 60
    turn_count: int = 180
    required_capabilities: frozenset[str] = frozenset(item.id for item in CAPABILITIES)


def failed_contract_metrics(metrics: dict[str, object]) -> list[str]:
    failed = []
    for name, threshold in SCRIPTED_THRESHOLDS.items():
        value = metrics.get(name)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not threshold <= value <= 1.0):
            failed.append(name)
    return failed


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
    false_mutation_count: int = 0
    unauthorized_execution_count: int = 0
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
    def __init__(self, *, corpus_version: str, allow_live: bool = False,
                 contract: EvaluationContract | None = None) -> None:
        self.corpus_version = corpus_version
        self.allow_live = allow_live
        self.contract = contract or EvaluationContract()

    def run(self, turns: Sequence[EvaluationTurn], adapter: EvaluationAdapter,
            *, output_path: Path | None = None) -> dict[str, object]:
        if adapter.mode not in {"offline_scripted", "live", "shadow"}:
            raise ValueError("unsupported evaluation mode")
        if adapter.mode in {"live", "shadow"} and not self.allow_live:
            raise PermissionError("live/shadow evaluation requires an explicit flag")
        counters = {name: 0 for name in (
            "route", "entity", "reference", "clarification", "argument", "memory", "safety"
        )}
        rows: list[dict[str, object]] = []
        totals = dict(side_effect_count=0, false_mutation_count=0, unauthorized_execution_count=0)
        for turn in turns:
            started = monotonic()
            actual = adapter.evaluate(turn)
            for name in totals:
                value = getattr(actual, name)
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid evaluation safety counter")
                totals[name] += value
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
                "safety": actual.safety_ok and all(getattr(actual, name) == 0 for name in totals),
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
        failed = failed_contract_metrics(metrics)
        dialogue_count = len({turn.dialogue_id for turn in turns})
        covered = {turn.expected.capability_id for turn in turns if turn.expected.capability_id}
        if dialogue_count != self.contract.dialogue_count:
            failed.append("dialogue_count")
        if total != self.contract.turn_count:
            failed.append("turn_count")
        if covered != self.contract.required_capabilities:
            failed.append("capability_coverage")
        if len({(turn.dialogue_id, turn.turn_id) for turn in turns}) != total:
            failed.append("unique_turn_ids")
        failed.extend(name for name, value in totals.items() if value != 0)
        report: dict[str, object] = {
            "corpus_version": self.corpus_version,
            "provider_mode": adapter.mode,
            "dialogue_count": dialogue_count,
            "turn_count": total,
            "metrics": metrics,
            "thresholds": SCRIPTED_THRESHOLDS,
            "safety_counters": totals,
            "failed_metrics": failed,
            "contract_pass": bool(total and not failed),
            "rows": rows,
        }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 1_000_000:
            raise ValueError("evaluation report exceeds bounded size")
        if output_path is not None:
            output_path.write_text(encoded + "\n", encoding="utf-8")
        return report
