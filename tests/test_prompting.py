"""Tests for src/prompting.py — pure string composition, ZERO imports.

tests/fixtures/golden_prompts_v1.json is the v1.0.0 REGRESSION GUARD: it was
captured from the unmodified v1.0.0 src/brain.py:16-41 + :75-80 concatenation
(SYSTEM_PROMPT + LENGTH_DIRECTIVES + the MEETING CONTEXT block), BEFORE
prompting.py or the worker refactor existed, by literally reproducing those
constants and that concatenation logic in a throwaway script and dumping the
result to JSON (never by importing brain.py, which pulls in google.genai).
Everything below in slice 3 is validated against these four exact strings.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import prompting

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "golden_prompts_v1.json").read_text(encoding="utf-8"))


# --- v1.0.0 regression guard (task 3.1) ------------------------------------


def test_golden_short_no_context_byte_identical_to_v1():
    result = prompting.compose_system_instruction("", "", "en", "short")
    assert result == GOLDEN["short_no_context"]


def test_golden_detailed_no_context_byte_identical_to_v1():
    result = prompting.compose_system_instruction("", "", "en", "detailed")
    assert result == GOLDEN["detailed_no_context"]


def test_golden_short_with_context_byte_identical_to_v1():
    context = "Casual job interview. The user is a software developer."
    result = prompting.compose_system_instruction("", context, "en", "short")
    assert result == GOLDEN["short_with_context"]


def test_golden_detailed_with_context_byte_identical_to_v1():
    context = "Sales call, be persuasive."
    result = prompting.compose_system_instruction("", context, "en", "detailed")
    assert result == GOLDEN["detailed_with_context"]


# --- answer-language x length combinations + unknown-language fallback (3.3) --


def test_spanish_answer_language_rule():
    result = prompting.compose_system_instruction("", "", "es", "short")
    assert "- ALWAYS reply in Spanish, no matter what." in result
    assert "- ALWAYS reply in English, no matter what." not in result


def test_unknown_language_falls_back_to_generic_rule():
    """A typo or unmapped code must degrade gracefully, never raise."""
    result = prompting.compose_system_instruction("", "", "fr", "short")
    assert "- ALWAYS reply in fr, no matter what." in result


@pytest.mark.parametrize("language", ["en", "es", "fr", "xx"])
@pytest.mark.parametrize("length", ["short", "detailed"])
def test_length_directive_present_for_every_language(language, length):
    result = prompting.compose_system_instruction("", "", language, length)
    assert prompting.LENGTH_DIRECTIVES[length].strip() in result


def test_unknown_length_omits_length_directive_without_raising():
    result = prompting.compose_system_instruction("", "", "en", "nonexistent")
    assert "LENGTH:" not in result


# --- preset context + call context join order ------------------------------


def test_preset_context_and_call_context_join_preset_first():
    result = prompting.compose_system_instruction(
        "Role: senior recruiter.", "Focus on Python roles.", "en", "short"
    )
    idx_preset = result.index("Role: senior recruiter.")
    idx_call = result.index("Focus on Python roles.")
    assert idx_preset < idx_call
    assert "MEETING CONTEXT (the topic and how the user wants to answer):" in result


def test_empty_preset_context_reproduces_v1_with_call_context_only():
    context = "Casual job interview. The user is a software developer."
    result = prompting.compose_system_instruction("", context, "en", "short")
    assert result == GOLDEN["short_with_context"]


def test_both_contexts_blank_omits_meeting_context_block():
    result = prompting.compose_system_instruction("  ", "  ", "en", "short")
    assert "MEETING CONTEXT (the topic and how the user wants to answer):" not in result
