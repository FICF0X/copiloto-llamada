"""Shared pytest fixtures.

The one gotcha every fixture here exists to work around: config.ROOT and every
*_FILE constant (src/config.py) are computed once, at import time. Monkeypatching
config.ROOT after that point does NOT move them - the already-imported *_FILE
Path objects keep pointing at the real project root. So fixtures patch each
*_FILE constant individually (they're plain module attributes, read fresh on
every access) and, for modules that REBIND their own path at import time
(settings.SETTINGS_FILE, and future presets.PRESETS_FILE), patch that
module-level name directly instead of config.ROOT.
"""
from __future__ import annotations

import pytest

from src import config, settings


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point every legacy *_FILE constant and settings.SETTINGS_FILE at tmp_path.

    None of the files are created - tests write only the ones they need, so a
    "file doesn't exist" legacy scenario is just "don't write it".
    """
    monkeypatch.setattr(config, "CONTEXT_FILE", tmp_path / "context.txt")
    monkeypatch.setattr(config, "DEVICE_FILE", tmp_path / "audio_device.txt")
    monkeypatch.setattr(config, "MODE_FILE", tmp_path / "listen_mode.txt")
    monkeypatch.setattr(config, "LENGTH_FILE", tmp_path / "answer_length.txt")
    monkeypatch.setattr(config, "HIDE_FILE", tmp_path / "hide_from_screenshare.txt")
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    return tmp_path
