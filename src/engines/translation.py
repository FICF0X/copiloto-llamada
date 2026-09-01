"""TranslatorStrategy: the Argos-backed engine. Zero Gemini calls, ever.

`__init__` takes NO `brain` and NO `usage` parameter - that omission is what
makes the free-tier guarantee STRUCTURAL rather than disciplinary: there is
no attribute on this class that could accidentally be called into a Gemini
request. See tests/test_engines_translation.py for the assertion that proves
it (inspect.signature + a spy Brain that fails on any attribute access).

Translator/Transcriber are constructor parameters, never imported at module
scope (only under TYPE_CHECKING, for type hints) - same seam
engines/assistant.py already uses, for the same reason: unit-testable with
4-line fakes and no argostranslate/faster-whisper installed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.engines.base import EngineCallbacks, EngineResult, Utterance

if TYPE_CHECKING:  # pragma: no cover - type-only, never imported at runtime
    from src.language_lock import LanguageLock
    from src.transcriber import Transcriber
    from src.translator import Translator


class TranslatorStrategy:
    """Transcribed utterance -> locked source language -> Argos translation.

    Owns the source-language decision (via `lock`) and the transcriber
    configuration that decision drives (`transcriber.language`) - the
    transcriber is an honest constructor dependency, not something reached
    through the listener, precisely so this class can be unit-tested with a
    3-line fake and so the decision lives in one obvious place.
    """

    kind = "translator"

    def __init__(
        self,
        translator: "Translator",
        transcriber: "Transcriber",
        target_lang: str,
        lock: "LanguageLock",
    ) -> None:
        self.translator = translator
        self.transcriber = transcriber
        self.target_lang = target_lang
        self._init_session_state()
        self.lock = lock

    def _init_session_state(self) -> None:
        self._previous_language = getattr(self.transcriber, "language", None)
        self._route_failure = ""

    def start(self) -> None:
        """Called once, on the worker thread, before the capture loop.

        Synchronizes transcriber.language with whatever this lock already
        knows (e.g. a manual override or a lock carried over from a prior
        utterance in the same session) - NOT a reset. Resetting the lock for
        a brand new session is the caller's job (chat_app's
        _start_listening, wired in slice 7), since mode/preset switching is
        idle-only and this strategy instance's lifetime is the caller's to
        manage, not this class's to assume.
        """
        # Remembered so close() can hand Whisper back the way it found it:
        # this transcriber is shared with the assistant engine, and leaving
        # it pinned to the call's language would garble the next assistant
        # session until the app restarted.
        self._init_session_state()
        self.transcriber.language = self.lock.locked or None

    def process(self, utterance: Utterance, cb: EngineCallbacks) -> EngineResult:
        if not self.lock.locked:
            newly_locked = self.lock.observe(utterance.language, utterance.language_probability)
            if newly_locked:
                # Feeds the lock back into Whisper: later passes skip
                # detection entirely, which is the latency win the lock
                # exists for in the first place.
                self.transcriber.language = newly_locked
            else:
                cb.on_status("🌐 Detectando idioma...")
                # The heard line already reached the UI via
                # utterance_detected (emitted by the worker before calling
                # process()); nothing is persisted here (chat_app's
                # `if result.primary` guard) and - critically - no package
                # gets downloaded for what might still be a misdetected
                # language.
                return EngineResult(source=utterance.text, primary="")

        source_lang = self.lock.locked

        # Resolved once per session, never per utterance: a failure here is
        # a blocking network call, and retrying it on every utterance would
        # stall the capture loop again and again while audio goes undrained.
        if not self._route_failure:
            try:
                self.translator.ensure_route(
                    source_lang, self.target_lang, on_status=cb.on_status
                )
            except Exception as exc:  # noqa: BLE001 - typed package errors
                self._route_failure = str(exc)
        if self._route_failure:
            # The message goes in `primary`: that is the field the UI shows,
            # so an error parked anywhere else reaches the user as a blank
            # bubble - exactly what the old print() to a missing console did.
            return EngineResult(
                source=utterance.text,
                primary=f"[{self._route_failure}]",
                source_language=source_lang,
                primary_language=self.target_lang,
                error=self._route_failure,
            )

        translated = self.translator.translate(utterance.text, source_lang, self.target_lang)

        return EngineResult(
            source=utterance.text,
            primary=translated,
            answer=translated,
            source_language=source_lang,
            primary_language=self.target_lang,
        )

    def close(self) -> None:
        """Give Whisper back the language setting this session borrowed.

        The transcriber is shared with the assistant engine. Left pinned to
        the call's detected language, the next assistant session would
        transcribe an English interviewer as phonetic nonsense, with nothing
        short of restarting the app to undo it.
        """
        self.transcriber.language = self._previous_language
