"""Safe local conversion of Telegram voice files for speech recognition."""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator


class AudioServiceError(RuntimeError):
    """Base error for local voice-audio processing."""


class FFmpegUnavailableError(AudioServiceError):
    """Raised when the ffmpeg executable cannot be found."""


class AudioConversionError(AudioServiceError):
    """Raised when ffmpeg cannot create a valid recognition WAV file."""


class AudioConversionTimeoutError(AudioConversionError):
    """Raised when ffmpeg exceeds its bounded execution time."""


@dataclass(frozen=True, slots=True)
class TemporaryAudioFiles:
    """Paths that live only for one voice-processing attempt."""

    source: Path
    wav: Path


@contextmanager
def temporary_audio_files() -> Iterator[TemporaryAudioFiles]:
    """Create isolated input/output files and always remove them afterwards."""

    with TemporaryDirectory(prefix="barbot-voice-") as directory:
        root = Path(directory)
        yield TemporaryAudioFiles(
            source=root / "telegram-voice.ogg",
            wav=root / "recognition.wav",
        )


class AudioService:
    """Convert arbitrary Telegram voice audio to Vosk-compatible PCM WAV."""

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._configured_ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds

    def find_ffmpeg(self) -> str | None:
        """Return an explicit executable or locate ffmpeg through PATH."""

        if self._configured_ffmpeg_path:
            return self._configured_ffmpeg_path
        return shutil.which("ffmpeg")

    def convert_to_wav(self, source: Path, target: Path) -> Path:
        """Create mono 16 kHz signed 16-bit little-endian PCM WAV."""

        ffmpeg = self.find_ffmpeg()
        if ffmpeg is None:
            raise FFmpegUnavailableError("ffmpeg executable was not found")
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            str(target),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioConversionTimeoutError(
                "ffmpeg conversion timed out"
            ) from exc
        except OSError as exc:
            raise AudioConversionError("ffmpeg could not be started") from exc

        if (
            result.returncode != 0
            or not target.is_file()
            or target.stat().st_size == 0
        ):
            raise AudioConversionError("ffmpeg did not create a valid WAV file")
        return target
