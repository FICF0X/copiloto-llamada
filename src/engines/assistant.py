"""AssistantStrategy: the Gemini-backed engine.

Moves everything Gemini-specific verbatim out of worker.py: the "Pensando..."
status, usage.record() at the same point in the sequence (before the Gemini
call, not after), the 429/RESOURCE_EXHAUSTED branch with its exact Spanish
copy, and "Traduciendo...". Behavior must be byte-identical to v1.0.0.

Brain/Translator/UsageTracker are constructor parameters, never imported at
module scope (only under TYPE_CHECKING, for type hints) — that is what lets
this module be unit-tested with 4-line fakes and no google.genai/argos
installed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.engines.base import EngineCallbacks, EngineResult, Utterance

if TYPE_CHECKING:  # pragma: no cover - type-only, never imported at runtime
    from src.brain import Brain
    from src.presets import Preset
    from src.translator import Translator
    from src.usage import UsageTracker


class AssistantStrategy:
    """Transcribed utterance -> streamed Gemini answer -> offline ES translation.

    `preset` is still duck-typed (reads only `.context` and
    `.answer_language` via getattr, never isinstance) even though slice 4
    wires in a real `src.presets.Preset` — this class needs no import of
    src.presets at runtime, and stays trivially fakeable with a
    SimpleNamespace in tests.
    """

    kind = "assistant"

    def __init__(
        self,
        preset: "Preset",
        brain: "Brain",
        translator: "Translator",
        usage: "UsageTracker",
        translate_answer_to: str = "",
    ) -> None:
        self.preset = preset
        self.brain = brain
        self.translator = translator
        self.usage = usage
        # Truthy target language string -> translate the finished answer.
        # Empty/falsy -> skip translation (used by future presets whose
        # answer language already equals the translation target).
        self.translate_answer_to = translate_answer_to

    def start(self) -> None:
        # Update the briefing but KEEP memory, so pause/resume doesn't lose
        # context. Memory is only cleared when a new conversation is started
        # (Brain.reset(), called elsewhere on "new conversation").
        self.brain.set_preset(
            getattr(self.preset, "context", ""),
            getattr(self.preset, "answer_language", "en"),
        )

    def process(self, utterance: Utterance, cb: EngineCallbacks) -> EngineResult:
        cb.on_status("Pensando...")
        # One answer = one real Gemini request. Count it (estimate).
        cb.on_usage(self.usage.record())

        # `pieces` feeds translation (matches v1.0.0: error text was streamed to
        # the UI but never translated nor counted as "the answer"). `display`
        # additionally carries any error text, because EngineResult.primary
        # must equal everything already pushed through cb.on_output — that is
        # what makes the UI's later `set_answer(result.primary)` a true no-op
        # repaint instead of erasing an error the user already saw stream in.
        pieces: list[str] = []
        display: list[str] = []
        failure = ""  # non-empty when Gemini failed; keeps errors out of history
        try:
            for piece in self.brain.answer_stream(utterance.text):
                pieces.append(piece)
                display.append(piece)
                cb.on_output(piece)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            failure = detail
            if "RESOURCE_EXHAUSTED" in detail or "429" in detail:
                cb.on_status("⚠️ Límite de Gemini alcanzado")
                error_text = (
                    "\n⚠️ Alcanzaste el límite de pedidos de Gemini. "
                    "Espera un minuto (límite por minuto) o prueba mañana "
                    "(límite diario)."
                )
            else:
                error_text = f"\n[error al consultar la IA: {exc}]"
            display.append(error_text)
            cb.on_output(error_text)

        # Translate the finished answer offline (no tokens). Whole-text
        # translation only: partial sentences translate poorly. Only the real
        # Gemini answer is ever fed to the translator — error copy is already
        # Spanish-facing and must never be run back through an EN->ES model.
        answer = "".join(pieces).strip()
        translation = ""
        if answer and self.translate_answer_to:
            cb.on_status("Traduciendo...")
            translation = self.translator.translate(answer)

        return EngineResult(
            source=utterance.text,
            # Not stripped: this must equal what was streamed, so re-setting
            # it in the UI cannot reflow or trim the bubble the user is
            # already reading.
            primary="".join(display),
            answer=answer,
            secondary=translation,
            primary_language=getattr(self.preset, "answer_language", "en"),
            error=failure,
        )

    def close(self) -> None:
        pass
