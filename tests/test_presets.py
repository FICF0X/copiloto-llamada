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

import pytest

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


def test_a_failed_write_is_reported_instead_of_silently_losing_the_preset(
    isolated_presets, monkeypatch
):
    """Settings can swallow a failed write; presets cannot.

    A preset holds prompt text the user typed. If the editor closed as if it
    had saved, the work would simply be gone at the next launch with nothing
    ever having said so.
    """
    from pathlib import Path

    def _raise_oserror(self, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _raise_oserror)

    with pytest.raises(presets.StorageError):
        presets.save([presets.Preset(id="x", label="hand-written preset")])


def test_a_duplicated_label_never_exceeds_the_length_the_editor_accepts(
    isolated_presets,
):
    """The generated copy name obeys the same cap a typed one does.

    Otherwise duplicating a long-named preset produces a row that saves fine
    and then refuses to be edited, for a rule the user never broke.
    """
    long_label = "P" * (presets.MAX_LABEL_LENGTH - 2)
    store = presets.load()
    original = presets.create(long_label, "context", "en", store)

    copy = presets.duplicate(original.id, presets.load())

    assert len(copy.label) <= presets.MAX_LABEL_LENGTH
    presets.update(copy.id, copy.label, "edited", "en", presets.load())


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


# --- Slice 5: full CRUD (create/update/duplicate/delete/restore) -----------
# Traces spec's preset-editor domain scenarios: "Create and use immediately",
# "Empty name blocked", "Duplicate rename blocked", "Last preset delete
# blocked", "Restore after editing a factory preset". Deleting-the-active-
# preset's fallback lives in chat_app.py (settings.preset_id is Qt-adjacent
# state this module never touches) and is not covered here - see the manual
# checklist in apply-progress.


def test_create_validates_and_persists(isolated_presets):
    rows = presets.load()
    created = presets.create("Sales Call", "Be persuasive.", "en", rows)

    assert created.label == "Sales Call"
    assert created.factory_id == ""
    assert created.builtin is False
    reloaded = presets.load()
    assert any(p.id == created.id for p in reloaded)


def test_create_rejects_empty_name(isolated_presets):
    rows = presets.load()
    try:
        presets.create("   ", "some context", "en", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass
    assert len(presets.load()) == 3  # nothing was created or persisted


def test_create_rejects_empty_context(isolated_presets):
    rows = presets.load()
    try:
        presets.create("Sales Call", "   ", "en", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass
    assert len(presets.load()) == 3


def test_create_rejects_duplicate_name_case_insensitive(isolated_presets):
    rows = presets.load()
    try:
        presets.create("interview", "some context", "en", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass


def test_create_rejects_name_over_length_bound(isolated_presets):
    rows = presets.load()
    try:
        presets.create("x" * (presets.MAX_LABEL_LENGTH + 1), "context", "en", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass


def test_update_renames_and_keeps_identity(isolated_presets):
    rows = presets.load()
    general = presets.find("", rows)
    original_id = general.id

    updated = presets.update(
        original_id, "General (mío)", "New context", "en", rows
    )

    assert updated.id == original_id  # spec: rename never changes identity
    reloaded = presets.load()
    renamed = presets.find(original_id, reloaded)
    assert renamed.label == "General (mío)"
    assert renamed.context == "New context"
    assert renamed.id == original_id


def test_update_rejects_rename_to_existing_name(isolated_presets):
    """spec: "Duplicate rename blocked" - renaming University to Interview
    is rejected and University keeps its name."""
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")
    university = next(p for p in rows if p.factory_id == "university")

    try:
        presets.update(
            university.id, interview.label, university.context, "es", rows
        )
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass

    reloaded = presets.load()
    still_university = presets.find(university.id, reloaded)
    assert still_university.label == "University (Spanish)"


def test_update_allows_keeping_its_own_name(isolated_presets):
    """Renaming a preset to the SAME name it already has must not be treated
    as a collision with itself."""
    rows = presets.load()
    general = presets.find("", rows)

    updated = presets.update(general.id, general.label, "New context", "en", rows)

    assert updated.label == general.label
    assert updated.context == "New context"


def test_update_rejects_unknown_id(isolated_presets):
    rows = presets.load()
    try:
        presets.update("does-not-exist", "Name", "context", "en", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass


def test_duplicate_creates_independent_user_owned_copy(isolated_presets):
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")

    copy = presets.duplicate(interview.id, rows)

    assert copy.id != interview.id
    assert copy.factory_id == ""  # never itself a factory row
    assert copy.builtin is False
    assert copy.context == interview.context
    assert copy.answer_language == interview.answer_language
    assert copy.label != interview.label  # auto-disambiguated
    reloaded = presets.load()
    assert len(reloaded) == 4


def test_duplicate_twice_gets_distinct_labels(isolated_presets):
    """Duplicating the same preset repeatedly must never collide - it's an
    automated action, the user never typed a name for it."""
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")

    first_copy = presets.duplicate(interview.id, rows)
    second_copy = presets.duplicate(interview.id, rows)

    assert first_copy.label != second_copy.label


def test_duplicate_rejects_unknown_id(isolated_presets):
    rows = presets.load()
    try:
        presets.duplicate("does-not-exist", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass


def test_delete_removes_and_persists(isolated_presets):
    rows = presets.load()
    interview = next(p for p in rows if p.factory_id == "interview")

    survivors = presets.delete(interview.id, rows)

    assert {p.factory_id for p in survivors} == {"general", "university"}
    reloaded = presets.load()
    assert {p.factory_id for p in reloaded} == {"general", "university"}


def test_delete_last_remaining_preset_is_blocked(isolated_presets):
    """spec: "Deleting the last preset is prevented"."""
    rows = [presets.Preset(id="only-one", label="Only One", context="x")]
    presets.save(rows)

    try:
        presets.delete("only-one", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass

    reloaded = presets.load()
    assert len(reloaded) == 1
    assert reloaded[0].id == "only-one"


def test_delete_unknown_id_is_rejected_without_mutating_the_store(isolated_presets):
    rows = presets.load()
    try:
        presets.delete("does-not-exist", rows)
        assert False, "expected ValidationError"
    except presets.ValidationError:
        pass
    assert len(presets.load()) == 3


def test_builtin_preset_is_fully_editable_and_deletable(isolated_presets):
    """`builtin` drives an editor badge, never a permission (design PINNED
    DECISION 2) - a factory preset goes through create/update/duplicate/
    delete with no special-casing whatsoever."""
    rows = presets.load()
    general = presets.find("", rows)
    assert general.builtin is True

    presets.update(general.id, "General (edited)", "edited context", "fr", rows)
    reloaded = presets.load()
    edited = presets.find(general.id, reloaded)
    assert edited.label == "General (edited)"
    assert edited.builtin is True  # still flagged as originally factory-seeded

    survivors = presets.delete(general.id, reloaded)
    assert all(p.id != general.id for p in survivors)


def test_restore_factory_presets_readds_only_missing_rows(isolated_presets):
    """spec: "Restore after editing a factory preset" - General was edited
    and Interview was deleted; restoring reverts General to nothing (it was
    only edited, not removed) while bringing Interview back, and never
    touches user-created presets."""
    rows = presets.load()
    general = presets.find("", rows)
    general.label = "General (edited by user)"
    general.context = "user's own context"
    without_interview = [p for p in rows if p.factory_id != "interview"]
    presets.save(without_interview)
    presets.create("My Own Preset", "user content", "en", without_interview)

    restored = presets.restore_factory_presets(without_interview)

    factory_ids = {p.factory_id for p in restored if p.factory_id}
    assert factory_ids == {"general", "interview", "university"}
    # General was edited but KEPT, so restore must not overwrite it.
    still_edited_general = next(p for p in restored if p.factory_id == "general")
    assert still_edited_general.label == "General (edited by user)"
    assert still_edited_general.context == "user's own context"
    # Interview was deleted, so restore re-adds a fresh factory copy.
    readded_interview = next(p for p in restored if p.factory_id == "interview")
    assert readded_interview.builtin is True
    assert readded_interview.context != ""
    # User-created preset is untouched.
    assert any(p.label == "My Own Preset" for p in restored)


def test_restore_factory_presets_is_idempotent(isolated_presets):
    rows = presets.load()
    without_any_factory = [p for p in rows if not p.factory_id]  # empty list

    first = presets.restore_factory_presets(without_any_factory)
    second = presets.restore_factory_presets(first)

    assert {p.factory_id for p in second} == {"general", "interview", "university"}
    assert len(second) == 3  # no duplicate rows on the second call
    assert [p.id for p in first] == [p.id for p in second]
