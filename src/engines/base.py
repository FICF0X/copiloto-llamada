"""Engine abstraction shared by every call mode: Utterance, EngineResult,
EngineCallbacks, EngineStrategy.

Imports: dataclasses, typing ONLY. Nothing below src/worker.py may import
PySide6, faster_whisper, pyaudiowpatch, google.genai or argostranslate at
module scope — that single rule is what makes this piece, and everything
built on top of it, unit-testable with no heavy dependency installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class Utterance:
    """One transcribed chunk of speech handed to an engine."""

    text: str
    language: str = ""  # transcriber.last_language for THIS utterance
    language_probability: float = 0.0


@dataclass(frozen=True)
class EngineResult:
    """What an engine produced from one Utterance.

    INVARIANT: `primary` always holds the complete text that must be visible,
    INCLUDING any error text the strategy already streamed. That lets the UI
    call one idempotent setter (ChatView.set_answer(result.primary)) for
    every engine kind, with no `if` on engine kind anywhere.
    """

    source: str  # what was heard
    primary: str = ""  # the exact full text to show in the output bubble
    secondary: str = ""  # Assistant: ES translation of the answer, or "". Translator: always ""
    source_language: str = ""  # detected/locked source (Translator), "" for Assistant
    primary_language: str = ""  # preset answer language / translator target
    answer: str = ""  # the engine's own output, WITHOUT any error copy:
    # this is what gets translated and saved. v1.0.0 gated persistence on
    # exactly this value, so an answer that streamed and then failed is
    # still kept, while a call that failed before producing anything is not.
    error: str = ""  # non-empty when the engine failed; UI may badge it


@dataclass
class EngineCallbacks:
    """Progress hooks an engine pushes through while processing one utterance."""

    on_output: Callable[[str], None]  # incremental output delta
    on_status: Callable[[str], None]  # user-facing status line (Spanish, per project convention)
    on_usage: Callable[[int], None]  # new estimated daily request count
    is_cancelled: Callable[[], bool]  # cooperative cancellation hook


class EngineStrategy(Protocol):
    """What CopilotWorker depends on. `kind` is label data, never branched on."""

    kind: str  # "assistant" | "translator"

    def start(self) -> None:
        """Called once, on the worker thread, before the capture loop starts."""
        ...

    def process(self, utterance: Utterance, cb: EngineCallbacks) -> EngineResult:
        """Turn one Utterance into an EngineResult, reporting progress via cb."""
        ...

    def close(self) -> None:
        """Called once when the capture loop ends (success, error, or cancel)."""
        ...
