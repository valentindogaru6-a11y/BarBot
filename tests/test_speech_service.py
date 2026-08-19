"""Tests for lazy Vosk use without a real model or native runtime."""

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.speech_service import (
    SpeechModelNotFoundError,
    SpeechRecognitionError,
    SpeechService,
    SpeechUnavailableError,
)


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 8000)


class _FakeRecognizer:
    def __init__(self, *args) -> None:
        self.args = args
        self.words_enabled = False

    def SetWords(self, enabled: bool) -> None:
        self.words_enabled = enabled

    def AcceptWaveform(self, chunk: bytes) -> bool:
        return False

    def Result(self) -> str:
        return json.dumps({"text": ""})

    def FinalResult(self) -> str:
        return json.dumps(
            {
                "text": "Олд Фэшн",
                "result": [
                    {"word": "олд", "conf": 0.8},
                    {"word": "фэшн", "conf": 1.0},
                ],
            }
        )


def test_model_is_loaded_lazily_and_reused(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    wav_path = tmp_path / "voice.wav"
    _write_wav(wav_path)
    model_calls: list[str] = []
    recognizers: list[_FakeRecognizer] = []

    def model(path: str) -> object:
        model_calls.append(path)
        return object()

    def recognizer(*args) -> _FakeRecognizer:
        instance = _FakeRecognizer(*args)
        recognizers.append(instance)
        return instance

    module = SimpleNamespace(Model=model, KaldiRecognizer=recognizer)
    service = SpeechService(model_path, module_loader=lambda _: module)
    assert not service.model_loaded

    first = service.transcribe(wav_path, ["олд фэшн", "[unk]"])
    second = service.transcribe(wav_path, ["олд фэшн"])

    assert first.text == "олд фэшн"
    assert first.language == "ru"
    assert first.confidence == pytest.approx(0.9)
    assert second.text == "олд фэшн"
    assert model_calls == [str(model_path)]
    assert service.model_loaded
    assert len(recognizers[0].args) == 3
    assert "олд фэшн" in recognizers[0].args[2]
    assert recognizers[0].words_enabled


def test_grammar_failure_falls_back_to_same_model(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    wav_path = tmp_path / "voice.wav"
    _write_wav(wav_path)
    attempts: list[int] = []

    def recognizer(*args):
        attempts.append(len(args))
        if len(args) == 3:
            raise RuntimeError("grammar unsupported")
        return _FakeRecognizer(*args)

    module = SimpleNamespace(
        Model=lambda _: object(),
        KaldiRecognizer=recognizer,
    )
    result = SpeechService(
        model_path,
        module_loader=lambda _: module,
    ).transcribe(wav_path, ["киви панч"])

    assert result.text == "олд фэшн"
    assert attempts == [3, 2]


def test_missing_model_is_reported_before_import(tmp_path: Path) -> None:
    loader_called = False

    def loader(_: str):
        nonlocal loader_called
        loader_called = True
        return SimpleNamespace()

    with pytest.raises(SpeechModelNotFoundError):
        SpeechService(tmp_path / "missing", module_loader=loader).transcribe(
            tmp_path / "voice.wav"
        )
    assert not loader_called


def test_missing_vosk_import_is_reported(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()

    def missing(_: str):
        raise ModuleNotFoundError("vosk")

    with pytest.raises(SpeechUnavailableError, match="vosk"):
        SpeechService(model_path, module_loader=missing).transcribe(
            tmp_path / "voice.wav"
        )


def test_model_load_failure_is_reported(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()

    def broken_model(_: str):
        raise RuntimeError("broken")

    module = SimpleNamespace(Model=broken_model)
    with pytest.raises(SpeechUnavailableError, match="model"):
        SpeechService(
            model_path,
            module_loader=lambda _: module,
        ).transcribe(tmp_path / "voice.wav")


def test_invalid_wav_format_is_rejected(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    wav_path = tmp_path / "voice.wav"
    with wave.open(str(wav_path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\x00\x00" * 100)
    module = SimpleNamespace(
        Model=lambda _: object(),
        KaldiRecognizer=_FakeRecognizer,
    )

    with pytest.raises(SpeechRecognitionError, match="mono"):
        SpeechService(
            model_path,
            module_loader=lambda _: module,
        ).transcribe(wav_path)
