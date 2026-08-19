"""Async boundary for blocking local audio and speech work."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Sequence

from app.services.audio_service import AudioService
from app.services.speech_service import SpeechRecognitionResult, SpeechService


class VoiceProcessingService:
    """Bound concurrent CPU-heavy recognition and keep the event loop free."""

    def __init__(
        self,
        audio_service: AudioService,
        speech_service: SpeechService,
        *,
        max_concurrent: int = 1,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be greater than zero")
        self.audio_service = audio_service
        self.speech_service = speech_service
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def recognize(
        self,
        source_path: Path,
        wav_path: Path,
        vocabulary: Sequence[str] | None = None,
    ) -> SpeechRecognitionResult:
        async with self._semaphore:
            await asyncio.to_thread(
                self.audio_service.convert_to_wav,
                source_path,
                wav_path,
            )
            return await asyncio.to_thread(
                self.speech_service.transcribe,
                wav_path,
                vocabulary,
            )
