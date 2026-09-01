"""The background thread that runs a call.

listen -> transcribe -> delegate to an EngineStrategy, off the UI thread.
Everything it learns is published as Qt signals, so a window can render the
call without knowing anything about audio, Whisper, Gemini or Argos.

The worker holds NO engine-specific branches: `strategy.kind` is label data,
never branched on here. What happens to a transcribed utterance is entirely
the injected EngineStrategy's decision.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.engines.base import EngineCallbacks, EngineStrategy, Utterance
from src.listener import Listener


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> delegate loop off the UI thread."""

    utterance_detected = Signal(str)  # renamed from question_detected
    partial_text = Signal(str)  # live transcription of what's being heard right now
    output_chunk = Signal(str)  # renamed from answer_chunk; Translator never emits it
    result_ready = Signal(object)  # carries an EngineResult
    hearing = Signal(str)  # live capture state: idle / speech / transcribing
    usage_updated = Signal(int)  # new estimated request count for today
    status = Signal(str)

    def __init__(
        self,
        listener: Listener,
        strategy: EngineStrategy,
        mode: str = "auto",  # "auto" = VAD per-utterance, "controlled" = stop-to-send
    ) -> None:
        super().__init__()
        self.listener = listener
        self.strategy = strategy
        self.mode = mode
        self.cancelled = False
        self._cb = EngineCallbacks(
            on_output=self.output_chunk.emit,
            on_status=self.status.emit,
            on_usage=self.usage_updated.emit,
            is_cancelled=lambda: self.cancelled,
        )

    def run(self) -> None:
        self.status.emit("Escuchando...")
        self.strategy.start()
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
            # source yields (text, language, language_probability) TOGETHER,
            # captured atomically inside the same transcribe() call that
            # produced the text - never re-read from self.listener.transcriber
            # after the fact. That used-to-be-implicit read was a real race:
            # Listener's live-preview thread mutates that same shared state
            # on its own daemon thread between a yield and this loop body
            # resuming, so an utterance could get tagged with a PREVIEW
            # pass' language instead of its own. See TranscriptionResult in
            # src/transcriber.py and the yields in src/listener.py.
            for text, language, language_probability in source:
                if self.cancelled:
                    break
                utt = Utterance(text, language, language_probability)
                self.utterance_detected.emit(utt.text)
                result = self.strategy.process(utt, self._cb)
                self.result_ready.emit(result)
                self.status.emit("Escuchando...")
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"Error: {exc}")
        finally:
            self.strategy.close()

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
