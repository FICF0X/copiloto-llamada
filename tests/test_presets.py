"""Tests for src/presets.py: presets.json store + factory-preset seeding.

Traces sdd/multi-mode/spec's assistant-presets scenarios ("Factory presets
available on first run") and design PINNED DECISION 2's consequences:
seeding is first-run only, restore semantics are NOT part of this slice
(that's 5.1), unknown fields are tolerated, and "General" reproduces v1.0.0
byte-for-byte (context="", answer_language="en").

CRUD (create/update/duplicate/delete) is exercised here at the module level
even though the editor UI for it lands in slice 5 — the tasks artifact asks
for the round-trip test seam to exist now. Since 4.1 only ships load/save/
find/translate_target, these round-trip tests operate directly on the
Preset dataclass + save()/load(), which is the full surface CRUD will sit on
top of.
"""
from __future__ import annotations

import json

from src import presets


# --- Factory seeding (spec: "Factory presets available on first run") -----


def test_fresh_install_seeds_general_interview_university(isolated_presets):
    rows = presets.load()

    labels = {p.label for p in rows}
    assert labels == {"General", "Interview", "University (Spanish)"}
    assert all(p.builtin for p in rows)
    assert presets.PRESETS_FILE.exists()


def test_general_factory_preset_reproduces_v1_exactly(isolated_presets):
    """context="" and answer_language="en" — the byte-identical v1.0.0
    regression guard (tests/test_prompting.py's golden fixture) depends on
    these two fields never drifting."""
    rows = presets.load()
    general = presets.find("", rows)

    assert general.factory_id == "general"
    assert general.context == ""
    assert general.answer_language == "en"


def test_university_preset_answers_in_spanish(isolated_presets):
    rows = presets.load()
    university = next(p for p in rows if p.factory_id == "university")

    assert university.answer_language == "es"
    assert university.context.strip() != ""


def test_seeding_happens_exactly_once(isolated_presets):
    """Loading twice must not append a second copy of each factory row -
    the second load() reads the file that the first load() wrote."""
    first = presets.load()
    second = presets.load()

    assert [p.id for p in first] == [p.id for p in second]
    assert len(second) == 3


def test_deleted_factory_preset_does_not_resurrect_on_next_launch(isolated_presets):
    """Seeding is first-run-only: reconciling on every launch would silently
    undo a user's deliberate deletion. Simulates that deletion by saving a
    file with "interview" already removed."""
    rows = presets.load()
    survivors = [p for p in rows if p.factory_id != "interview"]
    presets.save(survivors)

    reloaded = presets.load()

    assert {p.factory_id for p in reloaded} == {"general", "university"}
    assert len(reloaded) == 2


# --- Persistence round-trip (create/rename/duplicate/delete at the module
# level; the editor UI for these lands in slice 5) --------------------------


def test_create_round_trip(isolated_presets):
    rows = presets.load()
    rows.append(
        presets.Preset(
            id="user-1",
            label="Sales Call",
            context="Be persuasive.",
            answer_language="en",
        )
    )
    presets.save(rows)

    reloaded = presets.load()
    created = next(p for p in reloaded if p.id == "user-1")
    assert created.label == "Sales Call"
    assert created.context == "Be persuasive."
    assert created.builtin is False
    assert created.factory_id == ""


def test_rename_round_trip(isolated_presets):
    rows = presets.load()
    general = presets.find("", rows)
    general.label = "General (renamed)"
    presets.save(rows)

    reloaded = presets.load()
    renamed = presets.find("", reloaded)
    assert renamed.label == "General (renamed)"
    assert renamed.id == general.id  # rename never changes identity


def test_duplicate_round_trip(isolated_presets):
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")
    duplicate = presets.Preset(
        id="dup-1",
        label=f"{interview.label} (copia)",
        context=interview.context,
        answer_language=interview.answer_language,
        # A duplicate is a user-owned preset, never a second factory row.
        factory_id="",
        builtin=False,
    )
    rows.append(duplicate)
    presets.save(rows)

    reloaded = presets.load()
    assert len(reloaded) == 4
    dup = presets.find("dup-1", reloaded)
    assert dup.context == interview.context
    assert dup.factory_id == ""
    assert dup.builtin is False


def test_delete_round_trip(isolated_presets):
    rows = presets.load()
    survivors = [p for p in rows if p.factory_id != "university"]
    presets.save(survivors)

    reloaded = presets.load()
    assert {p.factory_id for p in reloaded} == {"general", "interview"}


# --- Tolerating unknown fields (forward compatibility) ----------------------


def test_unknown_json_fields_are_tolerated(isolated_presets):
    doc = {
        "schema": 1,
        "presets": [
            {
                "id": "abc",
                "factory_id": "",
                "label": "Custom",
                "engine_kind": "assistant",
                "context": "hi",
                "answer_language": "en",
                "builtin": False,
                "a_field_from_the_future": "must not crash the loader",
            }
        ],
    }
    presets.PRESETS_FILE.write_text(json.dumps(doc), encoding="utf-8")

    rows = presets.load()

    assert len(rows) == 1
    assert rows[0].id == "abc"
    assert rows[0].label == "Custom"


def test_corrupt_presets_json_is_quarantined_and_reseeded(isolated_presets):
    presets.PRESETS_FILE.write_text("{not valid json", encoding="utf-8")

    rows = presets.load()

    corrupt_path = presets.PRESETS_FILE.with_suffix(
        presets.PRESETS_FILE.suffix + ".corrupt"
    )
    assert corrupt_path.exists()
    assert {p.label for p in rows} == {"General", "Interview", "University (Spanish)"}


def test_write_swallows_oserror_on_read_only_root(isolated_presets, monkeypatch):
    from pathlib import Path

    def _raise_oserror(self, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _raise_oserror)

    presets.save([presets.Preset(id="x", label="won't persist, but won't crash")])


# --- find() resolution --------------------------------------------------


def test_find_empty_id_resolves_to_general(isolated_presets):
    rows = presets.load()
    resolved = presets.find("", rows)
    assert resolved.factory_id == "general"


def test_find_unknown_id_falls_back_to_general(isolated_presets):
    """An id that no longer exists (e.g. its preset was deleted elsewhere)
    must never leave the app pointing at nothing."""
    rows = presets.load()
    resolved = presets.find("does-not-exist", rows)
    assert resolved.factory_id == "general"


def test_find_known_id_returns_that_preset(isolated_presets):
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")
    resolved = presets.find(interview.id, rows)
    assert resolved is interview


def test_find_with_general_missing_falls_back_to_a_general_shaped_preset():
    """Even a hand-edited presets.json with "general" removed entirely must
    not crash - the app needs *a* General-shaped preset to fall back to."""
    rows = [presets.Preset(id="x", factory_id="interview", label="Interview")]
    resolved = presets.find("", rows)
    assert resolved.factory_id == "general"
    assert resolved.context == ""
    assert resolved.answer_language == "en"


# --- translate_target(): drives AssistantStrategy's translate_answer_to ----


def test_translate_target_skips_when_preset_already_answers_in_target_language():
    spanish_preset = presets.Preset(id="x", answer_language="es")
    assert presets.translate_target(spanish_preset, "es") == ""


def test_translate_target_returns_target_when_languages_differ():
    english_preset = presets.Preset(id="x", answer_language="en")
    assert presets.translate_target(english_preset, "es") == "es"


def test_translate_target_general_preset_matches_v1_hardcoded_es(isolated_presets):
    """Regression: v1.0.0 always translated to Spanish. General answers in
    English and the default translator_target is "es", so the derived value
    must equal the old hardcoded "es" for an unmodified install."""
    rows = presets.load()
    general = presets.find("", rows)
    assert presets.translate_target(general, "es") == "es"


def test_the_stand_in_general_keeps_a_stable_identity(isolated_presets):
    """A hand-edited presets.json without "general" must still resolve twice
    to the same preset: a fresh id per call would make the combo highlight a
    different row every time it repopulated."""
    survivors = [p for p in presets.load() if p.factory_id != "general"]
    presets.save(survivors)

    first = presets.find(presets.load(), "")
    second = presets.find(presets.load(), "")

    assert first.factory_id == "general"
    assert first.id == second.id
