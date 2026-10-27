import pytest

from scripts import evaluate_assistant_conversations as cli
from app.services.assistant.evaluation import ScriptedEvaluationAdapter
from app.services.assistant.live_evaluation import LiveRunOptions


@pytest.mark.parametrize('arguments', (
    ['--allow-live','--provider','groq','--max-provider-calls','20','--disable-telegram-transport'],
    ['--mode','live_isolated','--provider','groq','--max-provider-calls','20','--disable-telegram-transport'],
    ['--mode','live_isolated','--allow-live','--max-provider-calls','20','--disable-telegram-transport'],
    ['--mode','live_isolated','--allow-live','--provider','groq','--disable-telegram-transport'],
    ['--mode','live_isolated','--allow-live','--provider','groq','--max-provider-calls','0','--disable-telegram-transport'],
    ['--mode','live_isolated','--allow-live','--provider','groq','--max-provider-calls','20'],
    ['--mode','wrong'],
))
def test_invalid_cli_fails_before_keys_or_network(monkeypatch,capsys,arguments):
    def forbidden():raise AssertionError('keys loaded before gates')
    monkeypatch.setattr(cli,'_load_live_settings',forbidden)
    assert cli.main(arguments)==3
    assert 'failure' in capsys.readouterr().out


def test_live_cli_never_accepts_scripted_adapter(monkeypatch):
    monkeypatch.setattr(cli,'_load_live_settings',lambda:(_ for _ in ()).throw(AssertionError('key access')))
    assert cli.main(['--mode','live_isolated','--allow-live','--provider','groq',
        '--max-provider-calls','2','--disable-telegram-transport'],adapter=ScriptedEvaluationAdapter())==3


def test_temporary_factory_is_required_before_key_access():
    options=LiveRunOptions('live_isolated',True,20,'groq',True)
    with pytest.raises(ValueError):options.validate(None)


def test_scripted_mode_is_still_explicitly_offline(capsys):
    assert cli.main(['--mode','offline_scripted'])==0
    assert 'mode=offline_scripted' in capsys.readouterr().out


def test_unsafe_output_fails_before_key_access(monkeypatch):
    def forbidden():raise AssertionError('keys loaded')
    monkeypatch.setattr(cli,'_load_live_settings',forbidden)
    assert cli.main(['--mode','live_isolated','--allow-live','--provider','groq',
        '--max-provider-calls','20','--disable-telegram-transport','--output','/opt/barbot/data/catalog.json'])==3
