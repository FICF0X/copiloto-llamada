"""Tests for src/engines/assistant.py against fake Brain/Translator/UsageTracker
and fake EngineCallbacks. No google.genai, no argostranslate, no network.

Verifies AssistantStrategy reproduces v1.0.0's worker.py:48-97 sequence
exactly: status "Pensando..." -> usage.record() (before the Gemini call, not
after) -> streamed chunks -> the 429/RESOURCE_EXHAUSTED branch with its exact
Spanish copy -> "Traduciendo..." -> translate. And the EngineResult.primary
invariant: it always equals everything already pushed through cb.on_output.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.engines.assistant import AssistantStrategy
from src.engines.base import EngineCallbacks, Utterance


class FakeBrain:
    def __init__(self, chunks=(), raise_exc=None):
        self.chunks = chunks
        self.raise_exc = raise_exc
        self.preset_calls: list[tuple[str, str]] = []
        self.questions: list[str] = []

    def set_preset(self, preset_context: str, answer_language: str) -> None:
        self.preset_calls.append((preset_context, answer_language))

    def answer_stream(self, question: str):
        self.questions.append(question)
        for chunk in self.chunks:
            yield chunk
        if self.raise_exc is not None:
            raise self.raise_exc


class FakeTranslator:
    def __init__(self, result: str = "[es]"):
        self.result = result
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        return self.result


class FakeUsage:
    def __init__(self, count: int = 1):
        self.count = count
        self.record_calls = 0

    def record(self) -> int:
        self.record_calls += 1
        return self.count


def make_callbacks():
    events: list[tuple[str, object]] = []
    cb = EngineCallbacks(
        on_output=lambda t: events.append(("output", t)),
        on_status=lambda t: events.append(("status", t)),
        on_usage=lambda n: events.append(("usage", n)),
        is_cancelled=lambda: False,
    )
    return cb, events


def test_start_applies_preset_context_and_answer_language():
    brain = FakeBrain()
    preset = SimpleNamespace(context="Role: recruiter.", answer_language="es")
    strategy = AssistantStrategy(preset, brain, FakeTranslator(), FakeUsage(), "es")

    strategy.start()

    assert brain.preset_calls == [("Role: recruiter.", "es")]


def test_start_defaults_missing_preset_attributes():
    """Duck-typed preset: a bare object with no attributes must not raise."""
    brain = FakeBrain()
    strategy = AssistantStrategy(SimpleNamespace(), brain, FakeTranslator(), FakeUsage())

    strategy.start()

    assert brain.preset_calls == [("", "en")]


def test_success_path_streams_and_translates_in_order():
    brain = FakeBrain(chunks=["Hello", " world"])
    translator = FakeTranslator(result="Hola mundo")
    usage = FakeUsage(count=7)
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, translator, usage, "es"
    )
    cb, events = make_callbacks()

    result = strategy.process(Utterance("What time is it?"), cb)

    assert result.source == "What time is it?"
    assert result.primary == "Hello world"
    assert result.secondary == "Hola mundo"
    assert result.error == ""
    assert result.primary_language == "en"
    assert brain.questions == ["What time is it?"]
    assert translator.calls == ["Hello world"]
    assert usage.record_calls == 1
    # Exact v1.0.0 sequence: status, usage BEFORE the Gemini call, chunks, then
    # "Traduciendo..." before the translate call result lands in the return.
    assert events == [
        ("status", "Pensando..."),
        ("usage", 7),
        ("output", "Hello"),
        ("output", " world"),
        ("status", "Traduciendo..."),
    ]


def test_translate_answer_to_falsy_skips_translation():
    brain = FakeBrain(chunks=["Hi"])
    translator = FakeTranslator()
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, translator, FakeUsage(), ""
    )
    cb, events = make_callbacks()

    result = strategy.process(Utterance("hi"), cb)

    assert result.primary == "Hi"
    assert result.secondary == ""
    assert translator.calls == []
    assert ("status", "Traduciendo...") not in events


def test_empty_answer_skips_translation():
    brain = FakeBrain(chunks=[])
    translator = FakeTranslator()
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, translator, FakeUsage(), "es"
    )
    cb, events = make_callbacks()

    result = strategy.process(Utterance("..."), cb)

    assert result.primary == ""
    assert result.secondary == ""
    assert translator.calls == []


def test_resource_exhausted_error_uses_exact_spanish_copy_and_skips_translation():
    exc = RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
    brain = FakeBrain(chunks=[], raise_exc=exc)
    translator = FakeTranslator()
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, translator, FakeUsage(), "es"
    )
    cb, events = make_callbacks()

    result = strategy.process(Utterance("q"), cb)

    expected_error_text = (
        "\n⚠️ Alcanzaste el límite de pedidos de Gemini. "
        "Espera un minuto (límite por minuto) o prueba mañana "
        "(límite diario)."
    )
    # Displayed text is exactly what was streamed, leading newline included:
    # re-setting it in the UI must not trim the bubble the user is reading.
    assert result.primary == expected_error_text
    # Nothing was answered, so nothing is saved - v1.0.0 dropped this case.
    assert result.answer == ""
    # But nothing is ever translated: error copy is Spanish-facing already
    # and must never be fed back through the EN->ES translator.
    assert translator.calls == []
    assert result.secondary == ""
    assert ("status", "⚠️ Límite de Gemini alcanzado") in events
    assert ("output", expected_error_text) in events


def test_generic_error_uses_exact_copy():
    exc = ValueError("boom")
    brain = FakeBrain(chunks=["partial "], raise_exc=exc)
    translator = FakeTranslator(result="parcial")
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, translator, FakeUsage(), "es"
    )
    cb, events = make_callbacks()

    result = strategy.process(Utterance("q"), cb)

    assert result.primary == "partial \n[error al consultar la IA: boom]"
    # Translation still runs on the pre-error partial text only, exactly as
    # v1.0.0 did - the error suffix is never handed to the translator.
    assert translator.calls == ["partial"]
    assert result.secondary == "parcial"
    assert ("output", "\n[error al consultar la IA: boom]") in events


def test_usage_recorded_before_gemini_call_even_on_failure():
    """usage.record() must fire even when the stream raises immediately -
    matches v1.0.0 where the request is already spent by the time it fails."""
    brain = FakeBrain(chunks=[], raise_exc=RuntimeError("network down"))
    usage = FakeUsage(count=3)
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"), brain, FakeTranslator(), usage, "es"
    )
    cb, events = make_callbacks()

    strategy.process(Utterance("q"), cb)

    assert usage.record_calls == 1
    assert events[0] == ("status", "Pensando...")
    assert events[1] == ("usage", 3)


def test_close_is_a_noop():
    strategy = AssistantStrategy(
        SimpleNamespace(context="", answer_language="en"),
        FakeBrain(),
        FakeTranslator(),
        FakeUsage(),
    )
    strategy.close()  # must not raise


def test_kind_is_assistant_and_never_branched_on():
    assert AssistantStrategy.kind == "assistant"


def test_a_failed_gemini_call_is_reported_as_an_error_not_as_an_answer():
    """v1.0.0 showed failures on screen and never saved them.

    The UI persists a result only when `error` is empty, so a failure that
    left this field blank would file "limit reached" notices into the saved
    calls as if they were answers.
    """

    class FailingBrain:
        def set_context(self, context: str) -> None: ...
        def set_length(self, length: str) -> None: ...
        def set_preset(self, *args, **kwargs) -> None: ...

        def answer_stream(self, question: str):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    strategy = AssistantStrategy(
        preset=None,
        brain=FailingBrain(),
        translator=FakeTranslator(),
        usage=FakeUsage(),
        translate_answer_to="es",
    )
    result = strategy.process(Utterance(text="anything"), make_callbacks()[0])

    assert result.error, "a failed call must report an error"
    assert "límite" in result.primary.lower(), "the user still sees what happened"
    assert result.secondary == "", "error copy is never translated"
