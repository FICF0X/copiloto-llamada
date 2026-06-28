"""The AI brain: sends a transcribed question to Gemini and streams the answer back.

Keeps a short rolling conversation history so follow-up questions that refer back
to earlier turns ("about what you just mentioned, which was hardest?") make sense.
"""
from __future__ import annotations

from typing import Iterator

from google import genai
from google.genai import types

from src.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MAX_HISTORY_MESSAGES,
    validate,
)

SYSTEM_PROMPT = (
    "You are a real-time copilot helping the user answer questions during a live "
    "video call. You receive a transcription of what the other person said. "
    "Give a clear, accurate, ready-to-use answer the user can say out loud. "
    "You may also receive MEETING CONTEXT describing the topic of the call and how "
    "the user wants to answer (their background, tone, role). When present, follow "
    "it closely so the answer is specific to the user, not generic.\n"
    "Rules:\n"
    "- ALWAYS reply in English, no matter what.\n"
    "- Lead with the answer. Be direct and concise.\n"
    "- Use short paragraphs or bullet points; this is read at a glance.\n"
    "- If the transcription is not actually a question or seems cut off, say so "
    "in one line instead of inventing an answer."
)


class Brain:
    """Wraps the Gemini client and answers questions with conversation memory."""

    def __init__(self) -> None:
        validate()  # fail fast if the API key is missing
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.history: list[types.Content] = []  # rolling user/model turns
        self._context = ""  # static meeting briefing for this session

    def reset(self, context: str = "") -> None:
        """Wipe conversation memory and set the briefing. Call for a new conversation."""
        self.history = []
        self._context = context.strip()

    def set_context(self, context: str) -> None:
        """Update the meeting briefing WITHOUT clearing memory (pause/resume)."""
        self._context = context.strip()

    def _make_config(self) -> types.GenerateContentConfig:
        # The static meeting briefing rides in the system instruction (sent every
        # turn) instead of the history, so it never gets trimmed away.
        system = SYSTEM_PROMPT
        if self._context:
            system += (
                "\n\nMEETING CONTEXT (the topic and how the user wants to answer):\n"
                f"{self._context}"
            )
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.5,
            # Disable the model's internal "thinking" step for lower latency in a
            # live call. Set a positive budget if you want deeper reasoning instead.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
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

        stream = self.client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=self.history,
            config=self._make_config(),
        )
        pieces: list[str] = []
        for chunk in stream:
            if chunk.text:
                pieces.append(chunk.text)
                yield chunk.text

        # Remember our own answer so later questions can refer back to it.
        self.history.append(
            types.Content(role="model", parts=[types.Part(text="".join(pieces))])
        )
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
