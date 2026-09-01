"""The AI brain: sends a transcribed question to Gemini and streams the answer back.

Keeps a short rolling conversation history so follow-up questions that refer back
to earlier turns ("about what you just mentioned, which was hardest?") make sense.
"""
from __future__ import annotations

from typing import Iterator

from google import genai
from google.genai import types

from src import config
from src.config import GEMINI_MODEL, GEMINI_TIMEOUT_MS, MAX_HISTORY_MESSAGES, validate
from src.prompting import (
    ANSWER_LANGUAGE_RULES,
    BASE_PROMPT,
    LENGTH_DIRECTIVES,
    compose_system_instruction,
)

# Re-exported so brain.py:_test() below and any README references keep
# working. The actual strings/composition logic now live in src/prompting.py,
# which has ZERO imports and is testable without google.genai installed.
# This reproduces v1.0.0's SYSTEM_PROMPT exactly: base prompt + English rule.
SYSTEM_PROMPT = BASE_PROMPT.format(answer_language_rule=ANSWER_LANGUAGE_RULES["en"])


class Brain:
    """Wraps the Gemini client and answers questions with conversation memory."""

    def __init__(self) -> None:
        validate()  # fail fast if the API key is missing
        # Module attribute, not a `from ... import GEMINI_API_KEY` value: a key
        # entered at runtime (first-run dialog, see chat_app._prompt_for_api_key)
        # updates config.GEMINI_API_KEY after this module was already imported,
        # and only an attribute lookup like this one picks that update up.
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.history: list[types.Content] = []  # rolling user/model turns
        self._context = ""  # per-call briefing typed into the composer box
        self._preset_context = ""  # reusable role prompt carried by the active preset
        self._answer_language = "en"  # active preset's answer language
        self._length = "short"  # answer-length preference: "short" | "detailed"

    def set_length(self, length: str) -> None:
        """Set the answer-length preference ("short" or "detailed")."""
        if length in LENGTH_DIRECTIVES:
            self._length = length

    def reset(self, context: str = "") -> None:
        """Wipe conversation memory and set the briefing. Call for a new conversation."""
        self.history = []
        self._context = context.strip()

    def set_context(self, context: str) -> None:
        """Update the per-call briefing WITHOUT clearing memory (pause/resume).

        This is the free-text composer box, distinct from a preset's own
        context (set via set_preset) — both land in the same MEETING CONTEXT
        block of the composed prompt.
        """
        self._context = context.strip()

    def set_preset(self, preset_context: str, answer_language: str) -> None:
        """Apply the active preset's role prompt and answer language.

        Called once per listening session, before the loop starts (mirrors
        v1.0.0's set_context call at the top of CopilotWorker.run()).
        """
        self._preset_context = preset_context.strip()
        self._answer_language = answer_language

    def _make_config(self) -> types.GenerateContentConfig:
        # The static meeting briefing rides in the system instruction (sent every
        # turn) instead of the history, so it never gets trimmed away.
        system = compose_system_instruction(
            self._preset_context, self._context, self._answer_language, self._length
        )
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.5,
            # Disable the model's internal "thinking" step for lower latency in a
            # live call. Set a positive budget if you want deeper reasoning instead.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            # Abort the request if the API goes silent this long. Without it a
            # stalled stream blocks the worker thread — and the UI waiting on it
            # — with no way out but restarting the app.
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        )

    def _trim(self) -> None:
        """Keep only the most recent messages so token cost stays bounded."""
        if len(self.history) > MAX_HISTORY_MESSAGES:
            self.history = self.history[-MAX_HISTORY_MESSAGES:]

    def answer_stream(self, question: str) -> Iterator[str]:
        """Yield the answer in chunks, remembering the conversation so far."""
        self.history.append(
            types.Content(
                role="user",
                parts=[types.Part(text=f'The other person said: "{question}"')],
            )
        )
        self._trim()

        pieces: list[str] = []
        try:
            stream = self.client.models.generate_content_stream(
                model=GEMINI_MODEL,
                contents=self.history,
                config=self._make_config(),
            )
            for chunk in stream:
                if chunk.text:
                    pieces.append(chunk.text)
                    yield chunk.text
        finally:
            # Always close the turn, even when the request timed out or failed.
            # A user message left with no model reply after it would pair up with
            # the next question and poison every later request.
            if pieces:
                # Remember our own answer so later questions can refer back to it.
                self.history.append(
                    types.Content(role="model", parts=[types.Part(text="".join(pieces))])
                )
            elif self.history and self.history[-1].role == "user":
                self.history.pop()
            self._trim()

    def answer(self, question: str, context: str = "") -> str:
        """Return the full answer as a single string (resets memory first)."""
        self.reset(context)
        return "".join(self.answer_stream(question))


def _test() -> None:
    brain = Brain()
    brain.reset("Casual job interview. The user is a software developer.")

    q1 = "I built a real-time chat app while I was living in Miami."
    print(f"Q1: {q1}\nA1: ", end="")
    for piece in brain.answer_stream(q1):
        print(piece, end="", flush=True)

    q2 = "About what you just mentioned, what was the hardest part?"
    print(f"\n\nQ2 (follow-up): {q2}\nA2: ", end="")
    for piece in brain.answer_stream(q2):
        print(piece, end="", flush=True)

    print("\n\n[OK] If A2 refers to the chat app, conversation memory works.")


if __name__ == "__main__":
    _test()
