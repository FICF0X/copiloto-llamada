"""The AI brain: sends a transcribed question to Gemini and streams the answer back."""
from __future__ import annotations

from typing import Iterator

from google import genai
from google.genai import types

from src.config import GEMINI_API_KEY, GEMINI_MODEL, validate

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
    """Wraps the Gemini client and answers questions."""

    def __init__(self) -> None:
        validate()  # fail fast if the API key is missing
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.5,
            # Disable the model's internal "thinking" step for lower latency in a
            # live call. Set a positive budget if you want deeper reasoning instead.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

    def _build_prompt(self, question: str, context: str) -> str:
        if context.strip():
            return (
                f"MEETING CONTEXT (topic and how I want to answer):\n"
                f"{context.strip()}\n\n"
                f'The other person just said: "{question}"\n\n'
                "Give the answer I should say."
            )
        return question

    def answer_stream(self, question: str, context: str = "") -> Iterator[str]:
        """Yield the answer in chunks as Gemini generates it."""
        stream = self.client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=self._build_prompt(question, context),
            config=self._config,
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text

    def answer(self, question: str, context: str = "") -> str:
        """Return the full answer as a single string."""
        return "".join(self.answer_stream(question, context))


def _test() -> None:
    question = "What is the difference between a process and a thread?"
    print(f"QUESTION: {question}\n")
    print("ANSWER (streaming):\n")
    brain = Brain()
    for piece in brain.answer_stream(question):
        print(piece, end="", flush=True)
    print("\n\n[OK] Gemini responded - API key works.")


if __name__ == "__main__":
    _test()
