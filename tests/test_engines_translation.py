"""Tests for src/engines/translation.py against fake Translator/Transcriber
and a real LanguageLock (already unit-tested standalone in
tests/test_language_lock.py - reusing it here exercises the real
observe()/override() contract, not a re-implementation of it).

No argostranslate, no faster-whisper, no google.genai, no Qt.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from src.engines.base import EngineCallbacks, Utterance
from src.engines.translation import TranslatorStrategy
from src.language_lock import LanguageLock


class FakeTranslator:
    def __init__(
        self,
        translate_result: str = "[translated]",
        ensure_route_exc: Exception | None = None,
        route_kind: str = "direct",
    ):
        self.ensure_route_calls: list[tuple[str, str]] = []
        self.translate_calls: list[tuple[str, str, str]] = []
        self.ensure_route_exc = ensure_route_exc
        self.translate_result = translate_result
        # Mirrors the real Translator.ensure_route()'s Route return - only
        # `.kind` is read by the strategy (task 7.8's route-kind plumbing),
        # so a SimpleNamespace stands in rather than importing src.translator.
        self.route_kind = route_kind

    def ensure_route(self, from_code, to_code, on_status=None):
        self.ensure_route_calls.append((from_code, to_code))
        if on_status is not None:
            on_status("Preparando traduccion...")
        if self.ensure_route_exc is not None:
            raise self.ensure_route_exc
        return SimpleNamespace(kind=self.route_kind)

    def translate(self, text, from_code, to_code):
        self.translate_calls.append((text, from_code, to_code))
        return self.translate_result


class FakeTranscriber:
    def __init__(self):
        self.language = "unset"


def make_callbacks():
    events: list[tuple[str, object]] = []
    cb = EngineCallbacks(
        on_output=lambda t: events.append(("output", t)),
        on_status=lambda t: events.append(("status", t)),
        on_usage=lambda n: events.append(("usage", n)),
        is_cancelled=lambda: False,
    )
    return cb, events


# --- Structural, zero-Gemini guarantee --------------------------------------


def test_init_signature_has_no_brain_or_usage_parameter():
    """The free-tier guardrail is STRUCTURAL: there is no constructor slot
    at all for either dependency, so no code path inside this class could
    ever reach one."""
    params = set(inspect.signature(TranslatorStrategy.__init__).parameters)
    assert not ({"brain", "usage"} & params), (
        f"TranslatorStrategy.__init__ must not accept brain/usage, found: "
        f"{params & {'brain', 'usage'}}"
    )


def test_process_never_touches_a_brain_even_if_one_is_attached_afterwards():
    """Behavioral half of the guarantee: even a stray `strategy.brain`
    attribute set from outside must never be read by start()/process()/
    close(). A spy that raises on ANY attribute access proves it."""

    class ExplodingBrain:
        def __getattr__(self, name):
            raise AssertionError(
                f"TranslatorStrategy touched Brain.{name} - zero-Gemini guarantee violated"
            )

    translator = FakeTranslator()
    lock = LanguageLock(min_probability=0.70, min_votes=1)
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    strategy.brain = ExplodingBrain()  # a stray reference must still be inert

    strategy.start()
    strategy.process(Utterance("hello", "en", 0.95), make_callbacks()[0])
    strategy.close()  # no AssertionError anywhere above = the guarantee holds


def test_kind_is_translator():
    assert TranslatorStrategy.kind == "translator"


# --- Unlocked behavior: detecting, no download, nothing persisted -----------


def test_below_threshold_utterance_returns_empty_primary_and_detecting_status():
    translator = FakeTranslator()
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, events = make_callbacks()

    result = strategy.process(Utterance("bonjour", "fr", 0.30), cb)

    assert result.source == "bonjour"
    assert result.primary == ""
    assert ("status", "🌐 Detectando idioma...") in events
    assert translator.ensure_route_calls == []
    assert translator.translate_calls == []


def test_first_of_two_agreeing_confident_utterances_does_not_translate_yet():
    translator = FakeTranslator()
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("bonjour", "fr", 0.90), cb)

    assert result.primary == ""
    assert lock.locked == ""
    assert translator.translate_calls == []


# --- Locking feeds back into the transcriber ---------------------------------


def test_locking_sets_transcriber_language_and_then_translates():
    translator = FakeTranslator(translate_result="hola")
    transcriber = FakeTranscriber()
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    strategy = TranslatorStrategy(translator, transcriber, "es", lock)
    cb, _ = make_callbacks()

    strategy.process(Utterance("bonjour", "fr", 0.90), cb)  # 1st: not locked yet
    result = strategy.process(Utterance("bonjour", "fr", 0.85), cb)  # 2nd: locks

    assert lock.locked == "fr"
    assert transcriber.language == "fr"
    assert translator.ensure_route_calls == [("fr", "es")]
    assert translator.translate_calls == [("bonjour", "fr", "es")]
    assert result.primary == "hola"
    assert result.answer == "hola"
    assert result.source_language == "fr"
    assert result.primary_language == "es"
    assert result.error == ""


def test_already_locked_utterances_translate_directly_without_relocking():
    translator = FakeTranslator(translate_result="hola de nuevo")
    lock = LanguageLock(min_probability=0.70, min_votes=1)
    lock.observe("fr", 0.95)  # locks on the first sample (min_votes=1)
    assert lock.locked == "fr"
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, events = make_callbacks()

    result = strategy.process(Utterance("bonsoir", "fr", 0.10), cb)  # low prob, irrelevant once locked

    assert result.primary == "hola de nuevo"
    assert ("status", "🌐 Detectando idioma...") not in events
    assert translator.translate_calls == [("bonsoir", "fr", "es")]


# --- Manual override --------------------------------------------------------


def test_manual_override_before_any_utterance_translates_immediately():
    translator = FakeTranslator(translate_result="hallo")
    lock = LanguageLock()
    lock.override("de")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("guten tag", "fr", 0.10), cb)  # detection disagrees, irrelevant

    assert result.primary == "hallo"
    assert translator.ensure_route_calls == [("de", "es")]
    assert translator.translate_calls == [("guten tag", "de", "es")]


def test_start_synchronizes_transcriber_language_with_an_existing_lock():
    transcriber = FakeTranscriber()
    lock = LanguageLock()
    lock.override("pt")
    strategy = TranslatorStrategy(FakeTranslator(), transcriber, "es", lock)

    strategy.start()

    assert transcriber.language == "pt"


def test_start_sets_transcriber_language_to_none_when_unlocked():
    transcriber = FakeTranscriber()
    lock = LanguageLock()
    strategy = TranslatorStrategy(FakeTranslator(), transcriber, "es", lock)

    strategy.start()

    assert transcriber.language is None


# --- Route/install failures ---------------------------------------------------


def test_ensure_route_failure_is_shown_to_the_user_and_not_retried():
    translator = FakeTranslator(ensure_route_exc=RuntimeError("no hay paquete disponible"))
    lock = LanguageLock()
    lock.override("de")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("hallo", "de", 0.95), cb)
    strategy.process(Utterance("noch mal", "de", 0.95), cb)

    # The message rides in `primary` because that is the field the UI shows:
    # an error parked anywhere else reaches the user as a blank bubble.
    assert "no hay paquete disponible" in result.primary
    assert result.error == "no hay paquete disponible"
    assert result.source_language == "de"
    assert result.primary_language == "es"
    assert translator.translate_calls == []
    # Resolved once per session: retrying per utterance would block the
    # capture loop on the network again for every single thing said.
    assert len(translator.ensure_route_calls) == 1


def test_ensure_route_status_callback_is_forwarded_to_on_status():
    translator = FakeTranslator()
    lock = LanguageLock()
    lock.override("de")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, events = make_callbacks()

    strategy.process(Utterance("hallo", "de", 0.95), cb)

    assert ("status", "Preparando traduccion...") in events


# --- close() ------------------------------------------------------------------


# --- Route-kind plumbing (task 7.8: the pivot-quality warning has nothing
# to render until EngineResult carries the resolved route's kind) ----------


def test_route_kind_is_surfaced_on_a_successful_direct_result():
    translator = FakeTranslator(translate_result="hola", route_kind="direct")
    lock = LanguageLock()
    lock.override("fr")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("bonjour", "fr", 0.95), cb)

    assert result.route_kind == "direct"


def test_route_kind_is_surfaced_and_never_silent_on_a_pivot_result():
    translator = FakeTranslator(translate_result="hallo", route_kind="pivot")
    lock = LanguageLock()
    lock.override("fr")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "de", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("bonjour", "fr", 0.95), cb)

    assert result.route_kind == "pivot"


def test_route_kind_is_cached_and_not_rechecked_every_utterance():
    """Matches ensure_route's own once-per-session contract: the route's
    kind is resolved once, then reused - never re-derived from a second
    ensure_route call, which the existing 'resolved once per session' test
    already proves doesn't happen."""
    translator = FakeTranslator(translate_result="hola", route_kind="pivot")
    lock = LanguageLock()
    lock.override("fr")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    first = strategy.process(Utterance("bonjour", "fr", 0.95), cb)
    second = strategy.process(Utterance("bonsoir", "fr", 0.95), cb)

    assert first.route_kind == second.route_kind == "pivot"
    assert len(translator.ensure_route_calls) == 1


def test_route_kind_is_empty_while_still_unlocked():
    translator = FakeTranslator()
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    result = strategy.process(Utterance("bonjour", "fr", 0.30), cb)

    assert result.route_kind == ""


def test_close_is_safe_without_a_session():
    """close() restores the borrowed language; it must not blow up when no
    session ever started."""
    strategy = TranslatorStrategy(FakeTranslator(), FakeTranscriber(), "es", LanguageLock())
    strategy.close()  # must not raise


def test_closing_hands_whisper_back_the_language_it_had():
    """The transcriber is shared with the assistant engine.

    Left pinned to the call's language, the next assistant session would
    transcribe an English interviewer as phonetic nonsense, and nothing short
    of restarting the app would undo it.
    """
    transcriber = FakeTranscriber()
    transcriber.language = "en"
    lock = LanguageLock()
    lock.override("fr")
    strategy = TranslatorStrategy(FakeTranslator(), transcriber, "es", lock)

    strategy.start()
    assert transcriber.language == "fr", "the session pins Whisper while it runs"

    strategy.close()
    assert transcriber.language == "en", "and gives it back when it ends"


def test_changing_the_source_mid_session_resolves_the_new_pair():
    """A manual override changes the pair, so the route must be resolved again.

    Caching on "have we resolved once" meant the new pair was never
    installed: translate() then returned its unavailable placeholder and the
    UI recorded that string into the saved call as if it were a translation.
    """
    translator = FakeTranslator()
    lock = LanguageLock()
    lock.override("en")
    strategy = TranslatorStrategy(translator, FakeTranscriber(), "es", lock)
    cb, _ = make_callbacks()

    strategy.process(Utterance("hello", "en", 0.95), cb)
    lock.override("fr")
    strategy.process(Utterance("bonjour", "fr", 0.95), cb)

    assert translator.ensure_route_calls == [("en", "es"), ("fr", "es")]
