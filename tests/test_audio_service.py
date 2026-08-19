"""Tests for safe ffmpeg invocation and temporary audio cleanup."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.audio_service import (
    AudioConversionError,
    AudioConversionTimeoutError,
    AudioService,
    FFmpegUnavailableError,
    temporary_audio_files,
)


def test_ffmpeg_conversion_uses_bounded_argument_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.ogg"
    target = tmp_path / "target.wav"
    source.write_bytes(b"ogg")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        target.write_bytes(b"wav")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = AudioService(ffmpeg_path="/usr/bin/ffmpeg", timeout_seconds=12)

    assert service.convert_to_wav(source, target) == target
    assert captured["command"] == [
        "/usr/bin/ffmpeg",
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
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 12
    assert "shell" not in kwargs


def test_missing_ffmpeg_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.audio_service.shutil.which", lambda _: None)

    with pytest.raises(FFmpegUnavailableError):
        AudioService().convert_to_wav(
            tmp_path / "source.ogg",
            tmp_path / "target.wav",
        )


def test_ffmpeg_timeout_is_mapped(tmp_path: Path, monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AudioConversionTimeoutError):
        AudioService(ffmpeg_path="ffmpeg").convert_to_wav(
            tmp_path / "source.ogg",
            tmp_path / "target.wav",
        )


def test_nonzero_ffmpeg_result_is_mapped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(AudioConversionError):
        AudioService(ffmpeg_path="ffmpeg").convert_to_wav(
            tmp_path / "source.ogg",
            tmp_path / "target.wav",
        )


def test_temporary_audio_files_are_always_removed() -> None:
    source: Path
    wav: Path
    with pytest.raises(RuntimeError):
        with temporary_audio_files() as files:
            source = files.source
            wav = files.wav
            source.write_bytes(b"source")
            wav.write_bytes(b"wav")
            raise RuntimeError("stop")

    assert not source.exists()
    assert not wav.exists()
    assert not source.parent.exists()
