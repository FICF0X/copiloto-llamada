"""Tests for the platform dispatch logic in src/audio_source.py.

Only the dispatch (platform check) path is unit-testable here: real device
enumeration and capture need actual WASAPI hardware. `sys.platform` is
monkeypatched to a non-Windows value so these tests run without
pyaudiowpatch installed - the Windows import lives INSIDE `open_source`/
`list_devices`, so it is never reached on the non-Windows path.
"""
from __future__ import annotations

import sys

import pytest

from src import audio_source


def test_list_devices_raises_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(audio_source.UnsupportedPlatformError):
        audio_source.list_devices()


def test_open_source_raises_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(audio_source.UnsupportedPlatformError):
        audio_source.open_source(None)


def test_open_source_raises_with_device_index_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(audio_source.UnsupportedPlatformError):
        audio_source.open_source(3)


def test_a_missing_windows_backend_still_raises_unsupported_platform(monkeypatch):
    """A declared-but-uninstalled backend must not leak an ImportError.

    Callers only handle UnsupportedPlatformError; an ImportError escaping
    would surface as a raw traceback instead of the device dropdown's
    "(sin dispositivos: ...)" message.
    """
    import builtins

    monkeypatch.setattr(audio_source.sys, "platform", "win32")
    real_import = builtins.__import__

    def no_backend(name, *args, **kwargs):
        if name == "src.audio_capture":
            raise ImportError("No module named 'pyaudiowpatch'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_backend)

    with pytest.raises(audio_source.UnsupportedPlatformError):
        audio_source.list_devices()
    with pytest.raises(audio_source.UnsupportedPlatformError):
        audio_source.open_source(None)
