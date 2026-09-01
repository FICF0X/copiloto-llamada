"""Pure Gemini system-instruction composition. ZERO imports.

Moved out of brain.py (which imports google.genai at module level) so this
piece is testable with no SDK installed and no API key: it is the single
highest-value structural move for testability in the multi-mode refactor.

Order of concatenation MUST match v1.0.0's brain.py:75-80 exactly:
base(+language rule) + length directive + "\\n\\nMEETING CONTEXT (the topic
and how the user wants to answer):\\n" + context. This is the byte-identical
v1.0.0 regression guard covered by tests/test_prompting.py.
"""
from __future__ import annotations

# {answer_language_rule} is a slot: v1.0.0 hardcoded "- ALWAYS reply in
# English, no matter what." on this line. Filled from ANSWER_LANGUAGE_RULES
# below so the General preset (answer_language="en") reproduces it exactly.
BASE_PROMPT = (
    "You are a real-time copilot helping the user answer questions during a live "
    "video call. You receive a transcription of what the other person said. "
    "Give a clear, accurate, ready-to-use answer the user can say out loud. "
    "You may also receive MEETING CONTEXT describing the topic of the call and how "
    "the user wants to answer (their background, tone, role). When present, follow "
    "it closely so the answer is specific to the user, not generic.\n"
    "Rules:\n"
    "{answer_language_rule}\n"
    "- Lead with the answer. Be direct and concise.\n"
    "- Use short paragraphs or bullet points; this is read at a glance.\n"
    "- If the transcription is not actually a question or seems cut off, say so "
    "in one line instead of inventing an answer."
)

# Known answer-language codes. "en" reproduces v1.0.0's hardcoded line exactly.
ANSWER_LANGUAGE_RULES = {
    "en": "- ALWAYS reply in English, no matter what.",
    "es": "- ALWAYS reply in Spanish, no matter what.",
}

# Extra instruction appended per answer-length preference chosen in the UI.
# Verbatim from v1.0.0 brain.py:32-41.
LENGTH_DIRECTIVES = {
    "short": (
        "\n- LENGTH: keep it very brief — 1-2 sentences or a few short bullets. "
        "Give only what the user needs to say out loud, no preamble."
    ),
    "detailed": (
        "\n- LENGTH: give a thorough answer with the key supporting points, "
        "still structured to read at a glance."
    ),
}


def compose_system_instruction(
    preset_context: str, call_context: str, answer_language: str, length: str
) -> str:
    """Build the Gemini system instruction.

    preset_context is the reusable role prompt carried by the active preset;
    call_context is the free-text briefing typed into the composer box for
    this specific call. Both land in the same MEETING CONTEXT block (preset
    first, then the call's free text) so an empty preset_context reproduces
    v1.0.0's output byte-for-byte.

    Unknown answer_language codes fall back to a generic rule instead of
    raising, so a typo in a preset's language field never breaks the prompt.
    """
    rule = ANSWER_LANGUAGE_RULES.get(
        answer_language, f"- ALWAYS reply in {answer_language}, no matter what."
    )
    system = BASE_PROMPT.format(answer_language_rule=rule)
    system += LENGTH_DIRECTIVES.get(length, "")

    context = "\n".join(
        part for part in (preset_context.strip(), call_context.strip()) if part
    )
    if context:
        system += (
            "\n\nMEETING CONTEXT (the topic and how the user wants to answer):\n"
            f"{context}"
        )
    return system
