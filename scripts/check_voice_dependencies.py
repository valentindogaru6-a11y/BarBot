"""Read-only diagnostics for BarBot local voice-search dependencies."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECOMMENDED_MEMORY_MB = 512


def _configured_model_path() -> tuple[Path | None, str | None]:
    raw_path = os.environ.get(
        "VOSK_MODEL_PATH",
        "models/vosk-model-small-ru-0.22",
    )
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return None, "relative VOSK_MODEL_PATH escapes the project directory"
    return resolved, None


def _memory_mb() -> int | None:
    page_size = os.sysconf("SC_PAGE_SIZE")
    page_count = os.sysconf("SC_PHYS_PAGES")
    if not isinstance(page_size, int) or not isinstance(page_count, int):
        return None
    return (page_size * page_count) // (1024 * 1024)


def main() -> int:
    """Print dependency state without reading .env or downloading anything."""

    failures = 0
    print(f"Python: {sys.version.split()[0]}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        failures += 1
        print("[FAIL] ffmpeg: not found in PATH")
    else:
        try:
            version = subprocess.run(
                [ffmpeg, "-version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            failures += 1
            print("[FAIL] ffmpeg: executable could not be checked")
        else:
            first_line = version.stdout.splitlines()[0] if version.stdout else "unknown"
            if version.returncode == 0:
                print(f"[OK] ffmpeg: {first_line}")
            else:
                failures += 1
                print("[FAIL] ffmpeg: version command failed")

    if importlib.util.find_spec("vosk") is None:
        failures += 1
        print("[FAIL] vosk: Python package is not installed")
    else:
        print("[OK] vosk: Python package is importable")

    model_path, path_error = _configured_model_path()
    if path_error is not None or model_path is None:
        failures += 1
        print(f"[FAIL] model path: {path_error}")
    elif not model_path.is_dir():
        failures += 1
        print(f"[FAIL] model: directory not found: {model_path}")
    elif not all((model_path / child).is_dir() for child in ("am", "conf")):
        failures += 1
        print(f"[FAIL] model: incomplete Vosk directory: {model_path}")
    else:
        print(f"[OK] model: {model_path}")

    try:
        memory_mb = _memory_mb()
    except (OSError, ValueError):
        memory_mb = None
    if memory_mb is None:
        print("[WARN] RAM: could not determine total memory")
    elif memory_mb < RECOMMENDED_MEMORY_MB:
        print(
            f"[WARN] RAM: {memory_mb} MB; at least "
            f"{RECOMMENDED_MEMORY_MB} MB is recommended"
        )
    else:
        print(f"[OK] RAM: {memory_mb} MB total")

    if failures:
        print(f"Voice search is not ready: {failures} required check(s) failed.")
        return 1
    print("Voice search dependencies are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
