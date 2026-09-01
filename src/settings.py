"""Single settings.json store, seeded by additive migration from legacy .txt.

Replaces the scattered CONTEXT_FILE/DEVICE_FILE/MODE_FILE/LENGTH_FILE/HIDE_FILE
plain-text files (config.py:32-37) with one JSON document. Those legacy files
are the migration SOURCE, never the target: they are read once, never deleted,
never rewritten, so a rollback (deleting settings.json) returns the app to its
pre-migration state exactly.

Read order, most to least trusted:
1. settings.json exists and parses -> build field-by-field with raw.get(field,
   default). Unknown keys are ignored (forward compatibility); missing keys
   default. A newer schema is loaded as-is, never wiped.
2. Absent -> migrate_from_legacy(): seed from whichever legacy .txt files
   exist, default the rest, write once.
3. Corrupt (bad JSON, or not a dict) -> fall back to migrate_from_legacy(),
   NOT to defaults - the user's hand-written context.txt is their most
   valuable non-transcript state. The bad file is renamed to
   "settings.json.corrupt" once, then a fresh file is written.
4. Any OSError on write is swallowed, matching _write_text (chat_app.py) and
   config.set_api_key: the app must never die because ROOT is read-only.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from src import config

# Rebound rather than imported by value so tests can point the store
# elsewhere (config.ROOT and every *_FILE constant are computed at import
# time - see conversations.py's own comment for the same pattern).
SETTINGS_FILE = config.ROOT / "settings.json"

SCHEMA_VERSION = 1


@dataclass
class Settings:
    schema: int = SCHEMA_VERSION
    mode: str = "assistant"  # "assistant" | "translator"
    preset_id: str = ""  # "" -> resolve to the "general" factory preset
    # The per-call briefing box (chat_app.py's context_box). Distinct from a
    # preset's own context: this is the last free text typed into the composer.
    context: str = ""
    device_name: str = ""
    listen_mode: str = "controlled"  # capture style, unchanged meaning
    answer_length: str = "short"
    # None means "no user choice saved yet" -> caller falls back to
    # config.HIDE_FROM_SCREENSHARE.
    hide_from_screenshare: bool | None = None
    translator_target: str = "es"
    translator_source_override: str = ""


def load() -> Settings:
    """Load settings.json, migrating from legacy .txt files if needed."""
    try:
        raw_text = SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError:
        return migrate_from_legacy()

    try:
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise ValueError("settings.json does not contain a JSON object")
    except (json.JSONDecodeError, ValueError):
        _quarantine_corrupt_file()
        return migrate_from_legacy()

    return Settings(
        schema=raw.get("schema", SCHEMA_VERSION),
        mode=_valid_mode(raw.get("mode", "assistant")),
        preset_id=raw.get("preset_id", ""),
        context=raw.get("context", ""),
        device_name=raw.get("device_name", ""),
        listen_mode=_valid_listen_mode(raw.get("listen_mode", "controlled")),
        answer_length=_valid_length(raw.get("answer_length", "short")),
        hide_from_screenshare=raw.get("hide_from_screenshare", None),
        translator_target=raw.get("translator_target", "es"),
        translator_source_override=raw.get("translator_source_override", ""),
    )


def _valid_length(value: str) -> str:
    """Answer length, validated the way the app has always validated it."""
    value = (value or "").strip()
    return value if value in ("short", "detailed") else "short"


def _valid_listen_mode(value: str) -> str:
    """Capture style, defaulting the way the app has always defaulted it."""
    return (value or "").strip() or "controlled"


def _valid_mode(value: str) -> str:
    """Call mode (task 7.1's mode resolution): "assistant" or "translator",
    same validate-with-fallback shape as _valid_length/_valid_listen_mode -
    a hand-edited or corrupt settings.json must never crash the app or land
    on an unrecognized mode string the composer's mode selector can't render."""
    value = (value or "").strip()
    return value if value in ("assistant", "translator") else "assistant"


def save(s: Settings) -> None:
    """Write the whole settings document. Never raises on a read-only ROOT.

    Written atomically: the document goes to a temporary file in the same
    directory and is then swapped in with os.replace, which is atomic on
    Windows and POSIX alike. Five settings that used to live in five files
    now share one document, so a torn write (a forced quit, a laptop
    suspending, an antivirus holding the handle) would corrupt ALL of them
    at once - and the corrupt-file recovery falls back to the legacy .txt
    snapshot frozen at migration time, silently discarding everything
    changed since. The swap makes that unreachable: readers see either the
    old document or the new one, never a half-written one.
    """
    payload = json.dumps(asdict(s), ensure_ascii=False, indent=2)
    tmp_path = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, SETTINGS_FILE)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _quarantine_corrupt_file() -> None:
    """Rename a corrupt settings.json out of the way, once, for forensics.

    Lets migrate_from_legacy() write a fresh settings.json in its place
    without clobbering evidence of what went wrong.
    """
    corrupt_path = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".corrupt")
    try:
        SETTINGS_FILE.replace(corrupt_path)
    except OSError:
        pass


def migrate_from_legacy() -> Settings:
    """Seed Settings from whichever legacy .txt files exist, then save once.

    Each legacy file is independent - a partial set migrates fine. Legacy
    files are read-only inputs: never deleted, never rewritten, so re-running
    this (or deleting settings.json) is always safe.
    """
    s = Settings()

    context = _read_legacy(config.CONTEXT_FILE)
    if context is not None:
        s.context = context

    device_name = _read_legacy(config.DEVICE_FILE)
    if device_name is not None:
        s.device_name = device_name.strip()

    listen_mode = _read_legacy(config.MODE_FILE)
    if listen_mode is not None:
        s.listen_mode = _valid_listen_mode(listen_mode)

    length = _read_legacy(config.LENGTH_FILE)
    if length is not None:
        s.answer_length = _valid_length(length)

    hide = _read_legacy(config.HIDE_FILE)
    if hide is not None:
        hide = hide.strip()
        s.hide_from_screenshare = hide == "1" if hide in ("1", "0") else None

    save(s)
    return s


def _read_legacy(path) -> str | None:
    """Read a legacy .txt file's raw contents, or None if it doesn't exist."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
