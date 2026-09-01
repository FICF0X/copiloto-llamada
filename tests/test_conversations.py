"""Tests for src/conversations.py's mode/preset_id fields (task 3.15).

CONVERSATIONS_DIR is monkeypatched to tmp_path, same rebind pattern as
config.*_FILE and settings.SETTINGS_FILE (see conftest.py's module docstring):
conversations.py already rebinds it at import time from config.CONVERSATIONS_DIR
(conversations.py:16), so the fixture patches the module-level name directly.
"""
from __future__ import annotations

import json

import pytest

from src import conversations


@pytest.fixture
def isolated_conversations(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    return tmp_path / "conversations"


def test_new_conversation_defaults_to_assistant_mode_and_empty_preset(isolated_conversations):
    convo = conversations.new_conversation(context="hi")
    assert convo.mode == "assistant"
    assert convo.preset_id == ""


def test_save_and_load_round_trips_mode_and_preset_id(isolated_conversations):
    convo = conversations.new_conversation()
    convo.mode = "translator"
    convo.preset_id = "abc123"
    convo.exchanges.append({"question": "hola", "answer": "hi", "translation": ""})

    assert conversations.save(convo) is True
    loaded = conversations.load(convo.id)

    assert loaded is not None
    assert loaded.mode == "translator"
    assert loaded.preset_id == "abc123"


def test_pre_multi_mode_file_missing_fields_loads_with_defaults(isolated_conversations):
    """An old conversation JSON saved before this slice has no "mode"/
    "preset_id" keys at all - it must still load, defaulting both."""
    isolated_conversations.mkdir(parents=True, exist_ok=True)
    old_shape = {
        "id": "20250101-000000-000000",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
        "context": "old call",
        "exchanges": [{"question": "q", "answer": "a", "translation": "t"}],
        "custom_title": "",
    }
    (isolated_conversations / "20250101-000000-000000.json").write_text(
        json.dumps(old_shape), encoding="utf-8"
    )

    loaded = conversations.load("20250101-000000-000000")

    assert loaded is not None
    assert loaded.mode == "assistant"
    assert loaded.preset_id == ""
    assert loaded.exchanges == [{"question": "q", "answer": "a", "translation": "t"}]


def test_matches_still_searches_question_answer_translation_with_new_fields(
    isolated_conversations,
):
    convo = conversations.new_conversation()
    convo.mode = "translator"
    convo.exchanges.append({"question": "bonjour", "answer": "hello", "translation": ""})
    conversations.save(convo)

    assert convo.matches("bonjour") is True
    assert convo.matches("nothing here") is False


# --- Translator exchange shape (tasks 7.6/7.7) -------------------------------
#
# Spec's resolved question #2: a Translator exchange has NO question/answer
# fields - {heard_text, detected_source_language, translated_text,
# target_language, timestamp} instead, reusing the same per-file JSON store.


def test_translator_exchange_builds_the_spec_shape():
    exchange = conversations.translator_exchange(
        heard_text="bonjour", detected_source_language="fr",
        translated_text="hola", target_language="es", timestamp="2026-01-01T00:00:00",
    )

    assert exchange == {
        "heard_text": "bonjour",
        "detected_source_language": "fr",
        "translated_text": "hola",
        "target_language": "es",
        "timestamp": "2026-01-01T00:00:00",
    }
    assert "question" not in exchange
    assert "answer" not in exchange


def test_translator_exchange_defaults_timestamp_to_now():
    exchange = conversations.translator_exchange("bonjour", "fr", "hola", "es")
    assert exchange["timestamp"]  # non-empty, ISO-shaped


def test_translator_exchange_round_trips_through_save_and_load(isolated_conversations):
    convo = conversations.new_conversation()
    convo.mode = "translator"
    convo.exchanges.append(
        conversations.translator_exchange("bonjour", "fr", "hola", "es", "2026-01-01T00:00:00")
    )

    assert conversations.save(convo) is True
    loaded = conversations.load(convo.id)

    assert loaded is not None
    assert loaded.mode == "translator"
    assert loaded.exchanges == [
        {
            "heard_text": "bonjour",
            "detected_source_language": "fr",
            "translated_text": "hola",
            "target_language": "es",
            "timestamp": "2026-01-01T00:00:00",
        }
    ]


def test_title_derives_from_heard_text_for_a_translator_conversation(isolated_conversations):
    convo = conversations.new_conversation()
    convo.mode = "translator"
    convo.exchanges.append(
        conversations.translator_exchange("bonjour tout le monde", "fr", "hola a todos", "es")
    )

    assert convo.title == "bonjour tout le monde"


def test_matches_searches_translator_exchange_fields(isolated_conversations):
    convo = conversations.new_conversation()
    convo.mode = "translator"
    convo.exchanges.append(
        conversations.translator_exchange("bonjour", "fr", "hola mundo", "es")
    )

    assert convo.matches("bonjour") is True  # heard_text
    assert convo.matches("hola mundo") is True  # translated_text
    assert convo.matches("nothing here") is False
