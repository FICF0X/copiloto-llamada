"""Tests for src/settings.py: JSON store + additive legacy migration.

Traces spec settings-store scenarios: "Full legacy set migrates", "Partial
legacy set migrates", "Corrupt legacy file", "Idempotent on existing
settings.json", "Post-update continuity".
"""
from __future__ import annotations

import json

from src import config, presets, settings


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_full_legacy_set_migrates(isolated_settings):
    _write(config.CONTEXT_FILE, "Job interview for a backend role.")
    _write(config.DEVICE_FILE, "Speakers (Realtek)")
    _write(config.MODE_FILE, "auto")
    _write(config.LENGTH_FILE, "detailed")
    _write(config.HIDE_FILE, "1")

    s = settings.load()

    assert s.context == "Job interview for a backend role."
    assert s.device_name == "Speakers (Realtek)"
    assert s.listen_mode == "auto"
    assert s.answer_length == "detailed"
    assert s.hide_from_screenshare is True
    assert settings.SETTINGS_FILE.exists()


def test_partial_legacy_set_migrates(isolated_settings):
    _write(config.CONTEXT_FILE, "Some briefing")
    _write(config.DEVICE_FILE, "Headphones")
    # MODE_FILE, LENGTH_FILE, HIDE_FILE do not exist.

    s = settings.load()

    assert s.context == "Some briefing"
    assert s.device_name == "Headphones"
    assert s.listen_mode == "controlled"  # default
    assert s.answer_length == "short"  # default
    assert s.hide_from_screenshare is None  # caller falls back to config default


def test_no_legacy_files_defaults_everything(isolated_settings):
    s = settings.load()

    assert s == settings.Settings()
    assert settings.SETTINGS_FILE.exists()


def test_corrupt_legacy_answer_length_defaults_only_that_field(isolated_settings):
    _write(config.CONTEXT_FILE, "Kept fine")
    _write(config.LENGTH_FILE, "not-a-valid-length")

    s = settings.load()

    assert s.context == "Kept fine"
    assert s.answer_length == "short"  # invalid legacy value -> default, no crash


def test_idempotent_on_existing_settings_json(isolated_settings):
    _write(config.CONTEXT_FILE, "Original legacy context")
    first = settings.load()
    assert first.context == "Original legacy context"

    # User changes a value through the app; this is now the source of truth.
    first.context = "User edited this after migration"
    settings.save(first)

    # Legacy file is edited too (simulates a stale file lingering on disk) -
    # it must NOT be re-applied on a second load.
    _write(config.CONTEXT_FILE, "Stale legacy value that must be ignored")

    second = settings.load()

    assert second.context == "User edited this after migration"


def test_legacy_files_still_present_after_migration(isolated_settings):
    _write(config.CONTEXT_FILE, "Keep me")
    _write(config.DEVICE_FILE, "Keep me too")

    settings.load()

    assert config.CONTEXT_FILE.exists()
    assert config.CONTEXT_FILE.read_text(encoding="utf-8") == "Keep me"
    assert config.DEVICE_FILE.exists()
    assert config.DEVICE_FILE.read_text(encoding="utf-8") == "Keep me too"


def test_corrupt_settings_json_falls_back_to_legacy_migration_not_defaults(
    isolated_settings,
):
    _write(config.CONTEXT_FILE, "The user's most valuable state")
    settings.SETTINGS_FILE.write_text("{not valid json", encoding="utf-8")

    s = settings.load()

    assert s.context == "The user's most valuable state"


def test_corrupt_settings_json_is_quarantined_once(isolated_settings):
    settings.SETTINGS_FILE.write_text("{not valid json", encoding="utf-8")

    settings.load()

    corrupt_path = settings.SETTINGS_FILE.with_suffix(
        settings.SETTINGS_FILE.suffix + ".corrupt"
    )
    assert corrupt_path.exists()
    assert corrupt_path.read_text(encoding="utf-8") == "{not valid json"
    assert settings.SETTINGS_FILE.exists()  # fresh file written in its place


def test_settings_json_that_is_not_a_json_object_falls_back_to_legacy(
    isolated_settings,
):
    _write(config.CONTEXT_FILE, "Recovered from a non-object settings.json")
    settings.SETTINGS_FILE.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    s = settings.load()

    assert s.context == "Recovered from a non-object settings.json"


def test_newer_schema_file_loads_field_by_field_unwiped(isolated_settings):
    future_doc = {
        "schema": 99,
        "mode": "translator",
        "preset_id": "abc123",
        "context": "future context",
        "device_name": "future device",
        "listen_mode": "auto",
        "answer_length": "detailed",
        "hide_from_screenshare": True,
        "translator_target": "fr",
        "translator_source_override": "de",
        "a_field_that_does_not_exist_yet": "must be tolerated",
    }
    settings.SETTINGS_FILE.write_text(json.dumps(future_doc), encoding="utf-8")

    s = settings.load()

    assert s.schema == 99
    assert s.mode == "translator"
    assert s.preset_id == "abc123"
    assert s.context == "future context"
    assert s.translator_target == "fr"
    assert s.translator_source_override == "de"


def test_write_swallows_oserror_on_read_only_root(isolated_settings, monkeypatch):
    """A read-only ROOT must never crash the app (save() swallows OSError)."""
    from pathlib import Path

    def _raise_oserror(self, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _raise_oserror)

    settings.save(settings.Settings(context="won't persist, but won't crash"))


def test_save_is_atomic_so_a_failed_write_leaves_the_previous_file_intact(
    isolated_settings, monkeypatch
):
    """A torn write must never leave a half-written settings.json behind.

    All five settings share one document now, so a corrupt file loses them
    all at once and recovery falls back to the legacy snapshot. os.replace
    makes the swap atomic: readers see the old document or the new one.
    """
    settings.save(settings.Settings(context="ORIGINAL"))

    def boom(*args, **kwargs):
        raise OSError("disk full mid-write")

    monkeypatch.setattr(settings.os, "replace", boom)
    settings.save(settings.Settings(context="INTERRUPTED"))

    assert settings.SETTINGS_FILE.read_text(encoding="utf-8").count("ORIGINAL") == 1
    assert settings.load().context == "ORIGINAL"
    assert not settings.SETTINGS_FILE.with_suffix(".json.tmp").exists()


def test_load_validates_answer_length_like_the_legacy_migration_does(isolated_settings):
    """Both read paths must apply the same validation or they drift apart."""
    settings.SETTINGS_FILE.write_text(
        '{"schema": 1, "answer_length": "bogus", "listen_mode": ""}', encoding="utf-8"
    )
    loaded = settings.load()
    assert loaded.answer_length == "short"
    assert loaded.listen_mode == "controlled"


# --- preset_id resolution (task 4.6) ----------------------------------------
#
# settings.py itself does not know about presets.py (settings-store stays
# blast-radius-separated from the prompt library - PINNED DECISION 2). These
# tests exercise the seam where the two meet: settings.Settings.preset_id is
# just a stored string; presets.find() is what turns it into a real Preset.


def test_fresh_install_preset_id_is_empty_and_resolves_to_general(
    isolated_settings, isolated_presets
):
    s = settings.load()
    assert s.preset_id == ""  # a v1.0.0 user's migrated settings.json too

    active = presets.find(s.preset_id, presets.load())

    assert active.factory_id == "general"
    assert active.context == ""
    assert active.answer_language == "en"


def test_stored_preset_id_resolves_to_the_matching_preset(
    isolated_settings, isolated_presets
):
    rows = presets.load()
    university = next(p for p in rows if p.factory_id == "university")

    s = settings.Settings(preset_id=university.id)
    settings.save(s)

    reloaded = settings.load()
    active = presets.find(reloaded.preset_id, presets.load())

    assert active.id == university.id
    assert active.answer_language == "es"
