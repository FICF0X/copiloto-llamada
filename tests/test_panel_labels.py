"""Tests for src/panel_labels.py: per-mode LivePanel copy + the Translator
target-language catalog (task 7.3). Zero imports beyond the module itself -
no Qt, no chat_app.py (which cannot be imported in this environment; see
panel_labels.py's own module docstring).
"""
from __future__ import annotations

from src import panel_labels


def test_labels_for_assistant_matches_v1_copy():
    labels = panel_labels.labels_for(panel_labels.ASSISTANT)

    assert labels["header"] == "🎧 En vivo"
    assert labels["action_listening"] == "■ Enviar y responder"
    assert labels["action_done"] == "● Escuchar de nuevo"
    assert labels["hearing"]["capturing"] == (
        "🔴 Grabando... pulsa «Enviar» cuando termine la pregunta."
    )


def test_labels_for_translator_is_a_distinct_continuous_set():
    labels = panel_labels.labels_for(panel_labels.TRANSLATOR)

    assert labels["header"] != panel_labels.labels_for(panel_labels.ASSISTANT)["header"]
    assert labels["action_listening"] == "■ Detener"
    assert "🌐" in labels["hearing"]["capturing"]


def test_labels_for_unknown_mode_falls_back_to_assistant():
    assert panel_labels.labels_for("not-a-real-mode") == panel_labels.labels_for(
        panel_labels.ASSISTANT
    )


def test_every_mode_defines_the_same_label_keys():
    """Both label sets must be structurally interchangeable - chat_app reads
    the same keys regardless of which mode is active."""
    assistant_keys = set(panel_labels.labels_for(panel_labels.ASSISTANT))
    translator_keys = set(panel_labels.labels_for(panel_labels.TRANSLATOR))
    assert assistant_keys == translator_keys

    assistant_hearing_keys = set(panel_labels.labels_for(panel_labels.ASSISTANT)["hearing"])
    translator_hearing_keys = set(panel_labels.labels_for(panel_labels.TRANSLATOR)["hearing"])
    assert assistant_hearing_keys == translator_hearing_keys


def test_translator_target_languages_include_the_verified_direct_pairs():
    """Task 6.2's live Argos index query confirmed es<->en and es<->pt are
    DIRECT; the picker must offer both regardless of the pivot-only ones."""
    codes = [code for code, _ in panel_labels.TRANSLATOR_TARGET_LANGUAGES]
    assert "es" in codes
    assert "en" in codes
    assert "pt" in codes


def test_translator_target_languages_include_the_pivot_only_pairs():
    """fr/de/it have no direct pair to es (task 6.2) but still belong in the
    picker - they pivot through English (task 6.1 confirmed all three pivot
    legs are available) and surface the pivot-quality warning instead of
    being hidden."""
    codes = [code for code, _ in panel_labels.TRANSLATOR_TARGET_LANGUAGES]
    assert "fr" in codes
    assert "de" in codes
    assert "it" in codes


def test_translator_target_languages_have_no_duplicate_codes():
    codes = [code for code, _ in panel_labels.TRANSLATOR_TARGET_LANGUAGES]
    assert len(codes) == len(set(codes))


def test_language_label_returns_the_display_name():
    assert panel_labels.language_label("es") == "Español"
    assert panel_labels.language_label("fr") == "Francés"


def test_language_label_falls_back_to_the_code_itself_for_unknown_codes():
    assert panel_labels.language_label("xx") == "xx"
