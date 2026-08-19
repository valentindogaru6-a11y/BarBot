"""Tests for bounded asynchronous voice processing."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.services.speech_service import SpeechRecognitionResult
from app.services.voice_processing_service import VoiceProcessingService


class _ConcurrencyProbeAudio:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def convert_to_wav(self, source: Path, target: Path) -> Path:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return target


class _FakeSpeech:
    def transcribe(self, path: Path, vocabulary=None) -> SpeechRecognitionResult:
        return SpeechRecognitionResult("негрони", "ru", 1.0)


def test_concurrent_work_is_limited_by_semaphore() -> None:
    audio = _ConcurrencyProbeAudio()
    service = VoiceProcessingService(audio, _FakeSpeech(), max_concurrent=1)

    async def run() -> list[SpeechRecognitionResult]:
        return await asyncio.gather(
            service.recognize(Path("one.ogg"), Path("one.wav")),
            service.recognize(Path("two.ogg"), Path("two.wav")),
        )

    results = asyncio.run(run())

    assert audio.maximum == 1
    assert [result.text for result in results] == ["негрони", "негрони"]


def test_invalid_concurrency_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        VoiceProcessingService(
            _ConcurrencyProbeAudio(),
            _FakeSpeech(),
            max_concurrent=0,
        )
