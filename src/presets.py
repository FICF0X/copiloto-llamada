"""Single presets.json store: the assistant's reusable prompt library.

Sibling to settings.json (config.ROOT), never nested inside it. That blast-
radius separation is deliberate (sdd/multi-mode/design PINNED DECISION 2): a
settings.json write triggered by an `answer_length` toggle must never risk
the user's prompt library, and a corrupt presets.json must never take
device/context/mode settings down with it.

Factory presets (General, Interview, University) are SEEDED EDITABLE COPIES,
never an immutable list merged in at read time. Consequences, stated
precisely because they are easy to get wrong:

- Seeding happens ONLY when presets.json is ABSENT (first run / a fresh
  install). It must NOT reconcile on every launch — otherwise a factory
  preset the user deliberately deleted would resurrect itself every start.
- `restore_factory_presets()` (slice 5) is the only way to bring a deleted
  factory preset back, and it never overwrites one the user kept but edited.
- `factory_id` is provenance data ("" for user-created presets, the factory
  slug otherwise). `builtin` drives an editor badge and is NEVER a
  permission check — every preset, factory or not, is editable and
  deletable (full CRUD lands in slice 5; this slice only reads).

The "General" preset's `context`/`answer_language` MUST stay exactly
`("", "en")` — that is what makes a v1.0.0 user's migrated settings.json
(preset_id == "") resolve to a byte-identical experience. See the golden
fixture in tests/test_prompting.py for the regression guard.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass

from src import config

# Rebound rather than imported by value so tests can point the store
# elsewhere — same pattern settings.SETTINGS_FILE and conversations.py's
# CONVERSATIONS_DIR already document: config.ROOT and every *_FILE constant
# are computed once at import time, so a fixture must patch this name
# directly, never config.ROOT after the fact.
PRESETS_FILE = config.ROOT / "presets.json"

SCHEMA_VERSION = 1

# Factory definitions. Never mutated at runtime; _seed() and
# restore_factory_presets() (slice 5) build fresh Preset rows from these.
_FACTORY_DEFS: tuple[dict, ...] = (
    {
        "factory_id": "general",
        "label": "General",
        "engine_kind": "assistant",
        "context": "",
        "answer_language": "en",
    },
    {
        "factory_id": "interview",
        "label": "Interview",
        "engine_kind": "assistant",
        "context": (
            "You are helping the user in a live job interview. Give answers "
            "that sound confident, structured and natural to say out loud — "
            "not like a written essay."
        ),
        "answer_language": "en",
    },
    {
        "factory_id": "university",
        "label": "University (Spanish)",
        "engine_kind": "assistant",
        "context": (
            "You are helping the user follow a university lecture or "
            "seminar conducted in Spanish. Give answers that use precise, "
            "academic vocabulary appropriate for that setting."
        ),
        "answer_language": "es",
    },
)


@dataclass
class Preset:
    """One row in the prompt library.

    `id` is a uuid4 hex, generated once and kept forever — including across
    a rename or a "restore factory presets" re-seed — so it never collides
    with a duplicate and never needs to double as a display key.
    """

    id: str
    factory_id: str = ""  # "" for user-created presets; provenance only
    label: str = ""
    engine_kind: str = "assistant"  # "assistant" | "translator" (translator unused before slice 6/7)
    context: str = ""
    answer_language: str = "en"
    builtin: bool = False  # editor badge ONLY, never a permission check


def _new_id() -> str:
    return uuid.uuid4().hex


# Identity of the stand-in General used when presets.json has been hand-edited
# to remove the real one. Fixed rather than random so repopulating the combo
# keeps selecting the same row.
_ORPHAN_GENERAL_ID = "00000000000000000000000000000000"


def _factory_preset(defn: dict, preset_id: str = "") -> Preset:
    return Preset(
        id=preset_id or _new_id(),
        factory_id=defn["factory_id"],
        label=defn["label"],
        engine_kind=defn.get("engine_kind", "assistant"),
        context=defn["context"],
        answer_language=defn["answer_language"],
        builtin=True,
    )


def load() -> list[Preset]:
    """Load presets.json, seeding the factory presets on first run only.

    Read order mirrors settings.load(): parse an existing file field-by-field
    (unknown keys ignored, missing keys default — forward compatibility);
    absent -> seed; corrupt -> quarantine the bad file, then seed. Seeding
    NEVER happens just because a factory row is missing from an existing,
    otherwise-valid file — that is a user's deliberate deletion, not damage.
    """
    try:
        raw_text = PRESETS_FILE.read_text(encoding="utf-8")
    except OSError:
        return _seed()

    try:
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise ValueError("presets.json does not contain a JSON object")
        rows = raw.get("presets", [])
        if not isinstance(rows, list):
            raise ValueError("presets.json's 'presets' key is not a list")
    except (json.JSONDecodeError, ValueError):
        _quarantine_corrupt_file()
        return _seed()

    presets: list[Preset] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue  # a hand-corrupted row is skipped, not fatal to the rest
        presets.append(
            Preset(
                id=row["id"],
                factory_id=row.get("factory_id", ""),
                label=row.get("label", ""),
                engine_kind=row.get("engine_kind", "assistant"),
                context=row.get("context", ""),
                answer_language=row.get("answer_language", "en"),
                builtin=row.get("builtin", False),
            )
        )
    return presets


def _seed() -> list[Preset]:
    """First-run only: build the factory rows and persist them once."""
    presets = [_factory_preset(defn) for defn in _FACTORY_DEFS]
    save(presets)
    return presets


def save(presets: list[Preset]) -> None:
    """Write the whole presets document. Never raises on a read-only ROOT.

    Atomic write, the same pattern as settings.save(): the payload goes to a
    temp file in the same directory, then os.replace swaps it in — readers
    always see either the old document or the new one, never a torn one.
    """
    payload = json.dumps(
        {"schema": SCHEMA_VERSION, "presets": [asdict(p) for p in presets]},
        ensure_ascii=False,
        indent=2,
    )
    tmp_path = PRESETS_FILE.with_suffix(PRESETS_FILE.suffix + ".tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, PRESETS_FILE)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _quarantine_corrupt_file() -> None:
    """Rename a corrupt presets.json out of the way, once, for forensics."""
    corrupt_path = PRESETS_FILE.with_suffix(PRESETS_FILE.suffix + ".corrupt")
    try:
        PRESETS_FILE.replace(corrupt_path)
    except OSError:
        pass


def find(preset_id: str, presets: list[Preset]) -> Preset:
    """Resolve a stored preset_id to a Preset.

    Pure and parameterized (the `presets` list is passed in, never loaded
    internally) so callers control exactly which snapshot is searched and
    the function is trivially testable without touching disk.

    "" (a fresh install, or a v1.0.0 user's migrated settings.json) and any
    id that no longer exists (e.g. the active preset was deleted — full
    handling of that lands in slice 5) both resolve to the "general" factory
    preset, so the app is never left pointing at nothing.
    """
    if preset_id:
        for preset in presets:
            if preset.id == preset_id:
                return preset
    for preset in presets:
        if preset.factory_id == "general":
            return preset
    # presets.json was hand-edited to remove "general" entirely. Fall back to
    # a General-shaped preset rather than crash — with a FIXED id, because a
    # fresh uuid on every call would make the combo highlight a different row
    # each time it repopulated.
    general = next(d for d in _FACTORY_DEFS if d["factory_id"] == "general")
    return _factory_preset(general, preset_id=_ORPHAN_GENERAL_ID)


def translate_target(preset: Preset, translator_target: str) -> str:
    """The Argos target language to translate the answer into, or "" to skip.

    Skips translation when the preset's own answer language already equals
    the configured translator target — translating an already-Spanish
    answer into Spanish would re-run it through an EN->ES-oriented model for
    nothing and risk mangling it (spec: "No double translation when preset
    already answers in target language").
    """
    return "" if preset.answer_language == translator_target else translator_target
