"""Offline, language-agnostic speech recognition backed by a local Vosk model."""

from __future__ import annotations

import importlib
import json
import logging
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Sequence

logger = logging.getLogger(__name__)


class SpeechServiceError(RuntimeError):
    """Base error for local speech recognition."""


class SpeechUnavailableError(SpeechServiceError):
    """Raised when the local Vosk runtime is unavailable."""


class SpeechModelNotFoundError(SpeechUnavailableError):
    """Raised when the configured unpacked model directory is absent."""


class SpeechRecognitionError(SpeechServiceError):
    """Raised when a WAV file cannot be recognized safely."""


@dataclass(frozen=True, slots=True)
class SpeechRecognitionResult:
    """Normalized speech output with optional word-level confidence."""

    text: str
    language: str
    confidence: float | None = None


class SpeechService:
    """Lazily load and reuse one Vosk model for all recognition requests."""

    def __init__(
        self,
        model_path: Path,
        *,
        language: str = "ru",
        module_loader: Callable[[str], ModuleType] = importlib.import_module,
    ) -> None:
        self.model_path = model_path
        self.language = language
        self._module_loader = module_loader
        self._module: ModuleType | None = None
        self._model: object | None = None
        self._model_lock = threading.Lock()

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _get_runtime(self) -> tuple[ModuleType, object]:
        if self._module is not None and self._model is not None:
            return self._module, self._model
        with self._model_lock:
            if self._module is not None and self._model is not None:
                return self._module, self._model
            if not self.model_path.is_dir():
                raise SpeechModelNotFoundError(
                    f"Vosk model directory does not exist: {self.model_path}"
                )
            try:
                module = self._module_loader("vosk")
            except (ImportError, ModuleNotFoundError) as exc:
                raise SpeechUnavailableError(
                    "Python package 'vosk' is not installed"
                ) from exc
            try:
                model = module.Model(str(self.model_path))
            except Exception as exc:
                raise SpeechUnavailableError(
                    "Vosk model could not be loaded"
                ) from exc
            self._module = module
            self._model = model
            return module, model

    @staticmethod
    def _parse_result(raw_result: str) -> tuple[str, list[float]]:
        try:
            payload = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpeechRecognitionError(
                "Vosk returned malformed JSON"
            ) from exc
        text = str(payload.get("text", "")).strip()
        confidences = [
            float(word["conf"])
            for word in payload.get("result", [])
            if isinstance(word, dict) and "conf" in word
        ]
        return text, confidences

    @staticmethod
    def _create_recognizer(
        module: ModuleType,
        model: object,
        sample_rate: int,
        vocabulary: Sequence[str] | None,
    ) -> object:
        grammar = [phrase for phrase in (vocabulary or []) if phrase.strip()]
        if grammar:
            try:
                return module.KaldiRecognizer(
                    model,
                    sample_rate,
                    json.dumps(grammar, ensure_ascii=False),
                )
            except Exception as exc:
                logger.warning(
                    "Vosk grammar is unavailable; using unrestricted recognition (%s)",
                    type(exc).__name__,
                )
        try:
            return module.KaldiRecognizer(model, sample_rate)
        except Exception as exc:
            raise SpeechRecognitionError(
                "Vosk recognizer could not be initialized"
            ) from exc

    def transcribe(
        self,
        wav_path: Path,
        vocabulary: Sequence[str] | None = None,
    ) -> SpeechRecognitionResult:
        """Recognize one prepared mono PCM WAV file without network access."""

        module, model = self._get_runtime()
        try:
            audio = wave.open(str(wav_path), "rb")
        except (OSError, wave.Error) as exc:
            raise SpeechRecognitionError("WAV file could not be opened") from exc

        texts: list[str] = []
        confidences: list[float] = []
        try:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != 16000
                or audio.getcomptype() != "NONE"
            ):
                raise SpeechRecognitionError(
                    "WAV must be mono 16-bit PCM at 16 kHz"
                )
            recognizer = self._create_recognizer(
                module,
                model,
                audio.getframerate(),
                vocabulary,
            )
            set_words = getattr(recognizer, "SetWords", None)
            if callable(set_words):
                set_words(True)
            while chunk := audio.readframes(4000):
                if recognizer.AcceptWaveform(chunk):
                    text, scores = self._parse_result(recognizer.Result())
                    if text:
                        texts.append(text)
                    confidences.extend(scores)
            text, scores = self._parse_result(recognizer.FinalResult())
            if text:
                texts.append(text)
            confidences.extend(scores)
        except SpeechServiceError:
            raise
        except Exception as exc:
            raise SpeechRecognitionError("Vosk recognition failed") from exc
        finally:
            audio.close()

        normalized_text = " ".join(" ".join(texts).split()).casefold()
        confidence = (
            sum(confidences) / len(confidences) if confidences else None
        )
        return SpeechRecognitionResult(
            text=normalized_text,
            language=self.language,
            confidence=confidence,
        )
