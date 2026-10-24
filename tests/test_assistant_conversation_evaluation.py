from pathlib import Path

import pytest

from app.services.assistant.evaluation import (
    ConversationEvaluationRunner,
    EvaluationExpectation,
    EvaluationTurn,
    EvaluationActual,
    EvaluationContract,
    ScriptedEvaluationAdapter,
    SCRIPTED_THRESHOLDS,
    failed_contract_metrics,
)


def test_scripted_runner_scores_and_writes_only_explicit_output(tmp_path: Path) -> None:
    turns = tuple(
        EvaluationTurn("d1", f"t{index}", "synthetic", EvaluationExpectation(
            dialogue_act="new_request", domain="catalog", capability_id="catalog.get"
        )) for index in range(3)
    )
    runner = ConversationEvaluationRunner(corpus_version="v1", contract=EvaluationContract(
        dialogue_count=1, turn_count=3, required_capabilities=frozenset({'catalog.get'}),
    ))
    report = runner.run(turns, ScriptedEvaluationAdapter())
    assert report["provider_mode"] == "offline_scripted"
    assert report['contract_pass']
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


@pytest.mark.parametrize('metric', tuple(SCRIPTED_THRESHOLDS))
@pytest.mark.parametrize('bad', [None, float('nan'), float('inf'), -1, 0.9, '1.0', True])
def test_every_required_metric_fails_closed(metric,bad):
    metrics = dict(SCRIPTED_THRESHOLDS)
    metrics[metric] = bad
    assert metric in failed_contract_metrics(metrics)
    metrics.pop(metric)
    assert metric in failed_contract_metrics(metrics)


def test_route_zero_is_contract_failure():
    class Wrong(ScriptedEvaluationAdapter):
        def evaluate(self, turn):
            return EvaluationActual(dialogue_act='general_chat',domain='general',capability_id=None)
    turn = EvaluationTurn('d','t','synthetic',EvaluationExpectation('new_request','catalog','catalog.get'))
    runner = ConversationEvaluationRunner(corpus_version='test',contract=EvaluationContract(1,1,frozenset({'catalog.get'})))
    report=runner.run((turn,),Wrong())
    assert report['metrics']['route'] == 0.0
    assert not report['contract_pass']
    assert 'route' in report['failed_metrics']


@pytest.mark.parametrize('counter', ['side_effect_count','false_mutation_count','unauthorized_execution_count'])
def test_safety_violation_cannot_pass(counter):
    from dataclasses import asdict
    class Unsafe(ScriptedEvaluationAdapter):
        def evaluate(self,turn):
            return EvaluationActual(**asdict(turn.expected),**{counter:1})
    turn=EvaluationTurn('d','t','synthetic',EvaluationExpectation('new_request','catalog','catalog.get'))
    report=ConversationEvaluationRunner(corpus_version='test',contract=EvaluationContract(1,1,frozenset({'catalog.get'}))).run((turn,),Unsafe())
    assert not report['contract_pass']
    assert counter in report['failed_metrics']
    assert 'safety' in report['failed_metrics']


def test_incomplete_corpus_fails():
    turn=EvaluationTurn('d','t','synthetic',EvaluationExpectation('new_request','catalog','catalog.get'))
    report=ConversationEvaluationRunner(corpus_version='test').run((turn,),ScriptedEvaluationAdapter())
    assert not report['contract_pass']
    assert {'turn_count','dialogue_count','capability_coverage'} <= set(report['failed_metrics'])


@pytest.mark.parametrize('fail', [False,True])
def test_cli_exit_matches_full_frozen_contract(tmp_path,fail,capsys):
    import json
    from dataclasses import asdict
    from scripts.evaluate_assistant_conversations import main
    class Fake(ScriptedEvaluationAdapter):
        def evaluate(self,turn):
            fields=asdict(turn.expected)
            if fail:
                fields['domain']='wrong'
            return EvaluationActual(**fields)
    output=tmp_path/'evaluation.json'
    assert main(['--output',str(output)],adapter=Fake()) == int(fail)
    report=json.loads(output.read_text())
    assert report['contract_pass'] is (not fail)
    assert report['dialogue_count']==60 and report['turn_count']==180
    assert report['provider_mode']=='offline_scripted'
    assert ('route' in report['failed_metrics']) is fail
    assert 'offline_scripted' in capsys.readouterr().out


@pytest.mark.parametrize('failure',[False,True])
def test_cli_process_exit_code(failure):
    import subprocess
    import sys
    code = '''
from dataclasses import asdict
from scripts.evaluate_assistant_conversations import main
from app.services.assistant.evaluation import ScriptedEvaluationAdapter,EvaluationActual
class Fake(ScriptedEvaluationAdapter):
    def evaluate(self,turn):
        fields=asdict(turn.expected)
        if FAILURE:
            fields['domain']='wrong'
        return EvaluationActual(**fields)
raise SystemExit(main([],adapter=Fake()))
'''.replace('FAILURE',repr(failure))
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=False)
    assert result.returncode==int(failure)
    assert 'offline_scripted' in result.stdout
