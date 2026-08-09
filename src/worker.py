"""The background thread that runs a call.

listen -> transcribe -> answer -> translate, off the UI thread. Everything it
learns is published as Qt signals, so a window can render the call without
knowing anything about audio, Whisper or Gemini.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.brain import Brain
from src.listener import Listener
from src.translator import Translator
from src.usage import UsageTracker


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> answer -> translate loop off the UI thread."""

    question_detected = Signal(str)
    partial_text = Signal(str)  # live transcription of what's being heard right now
    answer_chunk = Signal(str)
    answer_done = Signal()
    translation_ready = Signal(str)
    exchange_recorded = Signal(str, str, str)  # question, answer, translation
    hearing = Signal(str)  # live capture state: idle / speech / transcribing
    usage_updated = Signal(int)  # new estimated request count for today
    status = Signal(str)

    def __init__(
        self,
        listener: Listener,
        brain: Brain,
        translator: Translator,
        usage: UsageTracker,
        context: str = "",
        mode: str = "auto",  # "auto" = VAD per-utterance, "controlled" = stop-to-send
    ) -> None:
        super().__init__()
        self.listener = listener
        self.brain = brain
        self.translator = translator
        self.usage = usage
        self.context = context
        self.mode = mode
        self.cancelled = False

    def run(self) -> None:
        self.status.emit("Escuchando...")
        # Update the briefing but KEEP memory, so pause/resume doesn't lose context.
        # Memory is only cleared when a new conversation is started.
        self.brain.set_context(self.context)
        # Controlled mode records everything until the user stops, then sends one
        # prompt; auto mode fires on every VAD-detected pause.
        if self.mode == "controlled":
            source = self.listener.capture_until_stop(
                on_state=self.hearing.emit, on_partial=self.partial_text.emit
            )
        else:
            source = self.listener.listen(
                on_state=self.hearing.emit, on_partial=self.partial_text.emit
            )
        try:
            for question in source:
                if self.cancelled:
                    break
                self.question_detected.emit(question)
                self.status.emit("Pensando...")
                # One answer = one real Gemini request. Count it (estimate).
                self.usage_updated.emit(self.usage.record())
                pieces: list[str] = []
                try:
                    for piece in self.brain.answer_stream(question):
                        pieces.append(piece)
                        self.answer_chunk.emit(piece)
                except Exception as exc:  # noqa: BLE001
                    detail = str(exc)
                    if "RESOURCE_EXHAUSTED" in detail or "429" in detail:
                        self.status.emit("⚠️ Límite de Gemini alcanzado")
                        self.answer_chunk.emit(
                            "\n⚠️ Alcanzaste el límite de pedidos de Gemini. "
                            "Espera un minuto (límite por minuto) o prueba mañana "
                            "(límite diario)."
                        )
                    else:
                        self.answer_chunk.emit(f"\n[error al consultar la IA: {exc}]")
                self.answer_done.emit()

                # Translate the finished answer offline (no tokens). Whole-text
                # translation only: partial sentences translate poorly.
                answer = "".join(pieces).strip()
                if answer:
                    self.status.emit("Traduciendo...")
                    translation = self.translator.translate(answer)
                    self.translation_ready.emit(translation)
                    self.exchange_recorded.emit(question, answer, translation)
                self.status.emit("Escuchando...")
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"Error: {exc}")

    def stop(self) -> None:
        """Finish the current capture and answer it."""
        self.listener.stop()

    def cancel(self) -> None:
        """Abandon this capture: no transcription, no question, no AI request.

        Checked at the top of the loop as well as inside the listener, so a
        cancel that lands while the final transcription is already running still
        stops short of spending a Gemini call on an answer nobody wants.
        """
        self.cancelled = True
        self.listener.cancel()
