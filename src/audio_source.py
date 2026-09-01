"""Platform-agnostic system audio capture seam.

`Listener` depends on this module's `SystemAudioSource` protocol and its
`open_source`/`list_devices` factory functions, never on a concrete backend
directly. Today only Windows (WASAPI, via `src.audio_capture`) is implemented,
but a new backend for another platform only needs to be added to the factory
below - nothing in `Listener`, any engine strategy, or preset code changes.

The Windows import is deferred INSIDE `open_source`/`list_devices` (not at
module scope) so this module imports cleanly on any platform, letting tests
monkeypatch `sys.platform` without pyaudiowpatch installed.
"""
from __future__ import annotations

import sys
from typing import Protocol

import numpy as np


class UnsupportedPlatformError(RuntimeError):
    """Raised when no `SystemAudioSource` backend exists for the current platform."""


class SystemAudioSource(Protocol):
    """A source of system audio, streamed as mono float32 chunks.

    This is `SystemAudioCapture`'s existing public surface (src/audio_capture.py)
    minus `stream()` (unused by `Listener`) and the pyaudio-flavoured
    `device_rate`/`channels` attributes (used only by that module's own
    `_level_test`). `SystemAudioCapture` satisfies this protocol as-is - no
    wrapper class is introduced.
    """

    device_name: str

    def start(self) -> None: ...

    def read(self) -> np.ndarray: ...  # mono float32 @ config.SAMPLE_RATE

    def stop(self) -> None: ...

    def __enter__(self) -> "SystemAudioSource": ...

    def __exit__(self, *exc) -> None: ...


def list_devices() -> list[dict]:
    """List the available system audio devices for the current platform.

    Each entry is {"index", "name", "is_default"}. Raises
    `UnsupportedPlatformError` on any platform without a backend.
    """
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"No system audio backend available for platform '{sys.platform}'."
        )
    try:
        from src.audio_capture import list_loopback_devices
    except ImportError as exc:  # backend declared but not installed
        raise UnsupportedPlatformError(
            f"The Windows audio backend is not installed: {exc}"
        ) from exc

    return list_loopback_devices()


def open_source(device_index: int | None) -> SystemAudioSource:
    """Build a fresh `SystemAudioSource` for the current platform.

    Raises `UnsupportedPlatformError` on any platform without a backend.
    """
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"No system audio backend available for platform '{sys.platform}'."
        )
    try:
        from src.audio_capture import SystemAudioCapture
    except ImportError as exc:  # backend declared but not installed
        raise UnsupportedPlatformError(
            f"The Windows audio backend is not installed: {exc}"
        ) from exc

    return SystemAudioCapture(device_index=device_index)
