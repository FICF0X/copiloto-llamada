"""Tests for src/worker.py's CopilotWorker dispatch loop.

CopilotWorker.run() is called DIRECTLY (never via .start()) so it executes
synchronously in the test's own thread: PySide6 Signal/slot connections
default to a Direct connection when emitter and receiver share a thread, so
no QThread, no event loop, and no QCoreApplication are needed to observe
emissions. This exercises the real dispatch code, not a re-implementation
of it.

A fake Listener + fake EngineStrategy stand in for real audio/Whisper and
real Brain/Translator - CopilotWorker holds no engine-specific branches, so
any object satisfying the EngineStrategy protocol drives it identically.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.engines.base import EngineResult, Utterance
from src.worker import CopilotWorker


class FakeListener:
    """Stands in for Listener: yields canned utterances, records call shape."""

    def __init__(self, utterances, transcriber=None):
        self.utterances = list(utterances)
        self.transcriber = transcriber or SimpleNamespace(
            last_language="en", last_language_probability=0.87
        )
        self.stop_called = False
        self.cancel_called = False
        self.capture_until_stop_calls = 0
        self.listen_calls = 0

    def capture_until_stop(self, on_state=None, on_partial=None):
        self.capture_until_stop_calls += 1
        yield from self.utterances

    def listen(self, on_state=None, on_partial=None):
        self.listen_calls += 1
        yield from self.utterances

    def stop(self) -> None:
        self.stop_called = True

    def cancel(self) -> None:
        self.cancel_called = True


class RaisingListener(FakeListener):
    """A source that yields once, then blows up mid-iteration."""

    def capture_until_stop(self, on_state=None, on_partial=None):
        self.capture_until_stop_calls += 1
        yield "ok"
        raise RuntimeError("mic died")


class FakeStrategy:
    """Records everything CopilotWorker does to it. `kind` is arbitrary label
    data on purpose - the worker must never branch on it."""

    def __init__(self, results, kind: str = "fake", on_process=None):
        self.kind = kind
        self.results = list(results)
        self.started = False
        self.closed = False
        self.processed: list[Utterance] = []
        self.cbs_seen = []
        self._on_process = on_process

    def start(self) -> None:
        self.started = True

    def process(self, utterance, cb):
        self.processed.append(utterance)
        self.cbs_seen.append(cb)
        if self._on_process is not None:
            self._on_process(utterance, cb)
        return self.results.pop(0)

    def close(self) -> None:
        self.closed = True


def _collect(worker: CopilotWorker) -> list[tuple]:
    events: list[tuple] = []
    worker.status.connect(lambda t: events.append(("status", t)))
    worker.utterance_detected.connect(lambda t: events.append(("utterance", t)))
    worker.output_chunk.connect(lambda t: events.append(("output", t)))
    worker.result_ready.connect(lambda r: events.append(("result", r)))
    worker.usage_updated.connect(lambda n: events.append(("usage", n)))
    return events


def test_controlled_mode_calls_capture_until_stop_not_listen():
    listener = FakeListener(["hi"])
    strategy = FakeStrategy([EngineResult(source="hi", primary="ans")])
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()

    assert listener.capture_until_stop_calls == 1
    assert listener.listen_calls == 0


def test_auto_mode_calls_listen_not_capture_until_stop():
    listener = FakeListener(["hi"])
    strategy = FakeStrategy([EngineResult(source="hi", primary="ans")])
    worker = CopilotWorker(listener, strategy, mode="auto")

    worker.run()

    assert listener.listen_calls == 1
    assert listener.capture_until_stop_calls == 0


def test_strategy_started_before_loop_and_closed_after():
    listener = FakeListener(["hi"])
    strategy = FakeStrategy([EngineResult(source="hi", primary="ans")])
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()

    assert strategy.started is True
    assert strategy.closed is True


def test_full_dispatch_signal_order_for_two_utterances():
    listener = FakeListener(["first", "second"])
    strategy = FakeStrategy(
        [
            EngineResult(source="first", primary="ans1"),
            EngineResult(source="second", primary="ans2"),
        ]
    )
    worker = CopilotWorker(listener, strategy, mode="controlled")
    events = _collect(worker)

    worker.run()

    kinds = [e[0] for e in events]
    assert kinds == [
        "status",  # "Escuchando..." at the very top
        "utterance",
        "result",
        "status",  # "Escuchando..." after utterance 1
        "utterance",
        "result",
        "status",  # "Escuchando..." after utterance 2
    ]
    assert events[0] == ("status", "Escuchando...")
    assert events[1] == ("utterance", "first")
    assert events[2][0] == "result" and events[2][1].primary == "ans1"
    assert events[4] == ("utterance", "second")
    assert events[5][0] == "result" and events[5][1].primary == "ans2"


def test_utterance_built_from_transcriber_language_fields():
    transcriber = SimpleNamespace(last_language="fr", last_language_probability=0.42)
    listener = FakeListener(["bonjour"], transcriber=transcriber)
    strategy = FakeStrategy([EngineResult(source="bonjour", primary="ans")])
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()

    utt = strategy.processed[0]
    assert utt.text == "bonjour"
    assert utt.language == "fr"
    assert utt.language_probability == 0.42


def test_result_ready_carries_the_exact_object_strategy_returned():
    canned = EngineResult(source="hi", primary="ans", secondary="respuesta")
    listener = FakeListener(["hi"])
    strategy = FakeStrategy([canned])
    worker = CopilotWorker(listener, strategy, mode="controlled")
    received = []
    worker.result_ready.connect(lambda r: received.append(r))

    worker.run()

    assert received == [canned]
    assert received[0] is canned


def test_engine_callbacks_route_to_the_matching_signals():
    def emit_progress(utt, cb):
        cb.on_output("chunk")
        cb.on_status("Pensando...")
        cb.on_usage(9)

    listener = FakeListener(["hi"])
    strategy = FakeStrategy(
        [EngineResult(source="hi", primary="ans")], on_process=emit_progress
    )
    worker = CopilotWorker(listener, strategy, mode="controlled")
    events = _collect(worker)

    worker.run()

    assert ("output", "chunk") in events
    assert ("status", "Pensando...") in events
    assert ("usage", 9) in events


def test_is_cancelled_callback_reflects_worker_cancelled_flag():
    seen = {}

    def check_cancelled(utt, cb):
        seen["value"] = cb.is_cancelled()

    listener = FakeListener(["hi"])
    strategy = FakeStrategy(
        [EngineResult(source="hi", primary="ans")], on_process=check_cancelled
    )
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()

    assert seen["value"] is False
    worker.cancelled = True
    assert worker._cb.is_cancelled() is True


def test_cancel_flag_breaks_loop_before_processing_next_utterance():
    def cancel_after_first(utt, cb):
        worker.cancelled = True

    listener = FakeListener(["first", "second", "third"])
    strategy = FakeStrategy(
        [EngineResult(source="first", primary="ans1")], on_process=cancel_after_first
    )
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()

    assert len(strategy.processed) == 1
    assert strategy.closed is True  # close() still runs via finally


def test_exception_mid_source_sets_error_status_and_still_closes_strategy():
    listener = RaisingListener(["ok"])
    strategy = FakeStrategy([EngineResult(source="ok", primary="ans")])
    worker = CopilotWorker(listener, strategy, mode="controlled")
    events = _collect(worker)

    worker.run()

    assert strategy.closed is True
    error_events = [e for e in events if e[0] == "status" and e[1].startswith("Error:")]
    assert len(error_events) == 1
    assert "mic died" in error_events[0][1]


def test_stop_calls_listener_stop_and_does_not_set_cancelled():
    listener = FakeListener([])
    worker = CopilotWorker(listener, FakeStrategy([]), mode="controlled")

    worker.stop()

    assert listener.stop_called is True
    assert worker.cancelled is False


def test_cancel_sets_flag_and_calls_listener_cancel():
    listener = FakeListener([])
    worker = CopilotWorker(listener, FakeStrategy([]), mode="controlled")

    worker.cancel()

    assert worker.cancelled is True
    assert listener.cancel_called is True


def test_kind_is_never_inspected_by_the_worker():
    """A strategy with an unrecognized `kind` dispatches identically - proof
    the worker holds no `if kind == ...` branching anywhere."""
    listener = FakeListener(["hi"])
    strategy = FakeStrategy([EngineResult(source="hi", primary="ans")], kind="translator")
    worker = CopilotWorker(listener, strategy, mode="controlled")

    worker.run()  # must not raise, must not behave differently

    assert strategy.started is True
    assert strategy.closed is True
    assert len(strategy.processed) == 1
