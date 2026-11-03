"""Offline scripted evaluation or explicitly authorized isolated live evaluation."""

from __future__ import annotations

import argparse
import asyncio
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


def main(argv: list[str] | None = None, *, adapter=None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid_cli_configuration')
    parser = SafeParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument('--mode', choices=('offline_scripted','live_isolated'),default='offline_scripted')
    parser.add_argument('--provider', choices=('groq','gemini'))
    parser.add_argument('--model')
    parser.add_argument('--max-provider-calls',type=int)
    parser.add_argument('--disable-telegram-transport',action='store_true')
    parser.add_argument('--gemini-fallback',action='store_true')
    parser.add_argument('--suite',choices=('smoke','core','extended'))
    parser.add_argument('--variant-seed',type=int)
    try:
        args = parser.parse_args(argv)
        if args.mode == 'live_isolated':
            from app.services.assistant.live_evaluation import LiveRunOptions,run_live,write_live_report,validate_report_output,budget_coverage
            from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
            from app.services.assistant.evaluation_budget import EvaluationSafetyError
            options = LiveRunOptions(args.mode,args.allow_live,args.max_provider_calls,
                args.provider,args.disable_telegram_transport,args.model,args.gemini_fallback,
                args.suite or 'smoke',args.variant_seed if args.variant_seed is not None else 0)
            from app.services.assistant.evaluation_corpus_v2 import select_suite
            suite,_ = select_suite(options.suite,options.variant_seed)
            coverage = budget_coverage(suite.cases)
            print('static_budget_estimate suite='+suite.name+' '+
                ' '.join(f'{name}={value}' for name,value in coverage.items()))
            if type(args.max_provider_calls) is int and args.max_provider_calls < coverage['minimum']:
                print(f"provider_budget_below_corpus_minimum minimum={coverage['minimum']}")
            options.validate(IsolatedEvaluationEnvironment)
            validate_report_output(args.output)
            if adapter is not None:
                raise ValueError('live_adapter_injection_denied')
            if args.max_provider_calls < coverage['estimated_normal']:
                print(f"partial_coverage_risk=true minimum={coverage['minimum']} estimated_normal={coverage['estimated_normal']}")
            report,code = asyncio.run(run_live(options,settings_loader=_load_live_settings))
            write_live_report(report,args.output)
            print(f"mode=live_isolated calls={report['call_budget']['attempted']} "
                  f"completed={report['completed']} skipped={report['skipped']} ready={report['ready']}")
            return code
        if (args.allow_live or args.provider or args.model or args.max_provider_calls is not None
            or args.disable_telegram_transport or args.gemini_fallback
            or args.suite is not None or args.variant_seed is not None
            or (adapter is not None and adapter.mode != 'offline_scripted')):
            raise ValueError('mode_mismatch')
    except Exception:
        print('evaluation_configuration_or_infrastructure_failure')
        return 3
    except BaseException as error:
        if isinstance(error,(KeyboardInterrupt,SystemExit,asyncio.CancelledError)):
            raise
        print('evaluation_safety_contract_failure')
        return 2
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
    ).run(turns, adapter or ScriptedEvaluationAdapter(), output_path=args.output)
    print(
        f"mode={report['provider_mode']} dialogues={report['dialogue_count']} "
        f"turns={report['turn_count']} contract_pass={report['contract_pass']}"
    )
    print("metrics=" + ",".join(
        f"{name}:{value:.3f}"
        for name, value in sorted(report["metrics"].items())
    ))
    print("failed_metrics=" + ",".join(report["failed_metrics"]))
    return 0 if report["contract_pass"] else 1


def _load_live_settings():
    from app.config import Settings
    return Settings()


if __name__ == "__main__":
    raise SystemExit(main())
