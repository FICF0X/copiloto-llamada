"""Tests for src/listener.py using an injected fake SystemAudioSource.

Before the audio-source seam, Listener() always opened a real WASAPI device
inside capture_until_stop()/listen(), which made its VAD/endpointing loop
untestable without hardware. Injecting a fake factory that yields canned
numpy frames makes the loop testable for the first time - these tests do NOT
touch pyaudiowpatch, webrtcvad's real speech detector, or faster-whisper.

Listener._is_speech is monkeypatched per test to make the VAD/endpointing
outcome deterministic: whether real audio "sounds like speech" to webrtcvad
is outside what this seam is responsible for.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from src import audio_source
from src.listener import Listener


class FakeTranscriber:
    """Stands in for Transcriber: no Whisper model, no GPU, no CUDA."""

    def __init__(self, text: str = "hello") -> None:
        self._text = text
        self.language: str | None = None
        self.last_language = ""

    def transcribe(self, audio: np.ndarray) -> str:
        return self._text


class FakeAudioSource:
    """A canned SystemAudioSource: returns pre-scripted chunks, then signals
    the Listener to stop (mirrors an external stop()/cancel() call arriving
    while a real cap.read() is in flight).
    """

    def __init__(self, chunks: list[np.ndarray]) -> None:
        self.chunks = chunks
        self.calls = 0
        self.started = False
        self.stopped = False
        self.device_name = "Fake Loopback Device"
        self.listener: Listener | None = None  # set by the test after construction

    def start(self) -> None:
        self.started = True

    def read(self) -> np.ndarray:
        idx = self.calls
        self.calls += 1
        if idx < len(self.chunks):
            return self.chunks[idx]
        assert self.listener is not None, "test must set fake_source.listener"
        self.listener.running = False
        return np.zeros(0, dtype=np.float32)

    def stop(self) -> None:
        self.stopped = True

    def __enter__(self) -> "FakeAudioSource":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def test_listener_default_open_source_is_audio_source_factory():
    """The default argument must be the real factory so every existing call
    site (chat_app.py, listener.py's own _test()) keeps compiling untouched.
    """
    sig = inspect.signature(Listener.__init__)
    assert sig.parameters["open_source"].default is audio_source.open_source


def test_listener_uses_injected_factory_with_device_index():
    fake_source = FakeAudioSource(chunks=[np.zeros(10, dtype=np.float32)])
    seen_device_indexes: list[int | None] = []

    def factory(device_index: int | None):
        seen_device_indexes.append(device_index)
        return fake_source

    listener = Listener(
        FakeTranscriber(),
        device_index=7,
        open_source=factory,
    )
    fake_source.listener = listener

    results = list(listener.capture_until_stop())

    assert seen_device_indexes == [7]
    assert fake_source.started is True
    assert fake_source.stopped is True
    assert results == ["hello"]


def test_capture_until_stop_transcribes_accumulated_frames():
    chunk1 = np.ones(50, dtype=np.float32)
    chunk2 = np.ones(25, dtype=np.float32) * 2
    fake_source = FakeAudioSource(chunks=[chunk1, chunk2])

    listener = Listener(
        FakeTranscriber(text="hello capture"),
        open_source=lambda device_index: fake_source,
    )
    fake_source.listener = listener

    results = list(listener.capture_until_stop())

    assert results == ["hello capture"]
    assert fake_source.stopped is True


def test_capture_until_stop_yields_nothing_when_cancelled():
    chunk1 = np.ones(50, dtype=np.float32)
    fake_source = FakeAudioSource(chunks=[chunk1])

    listener = Listener(
        FakeTranscriber(text="should not appear"),
        open_source=lambda device_index: fake_source,
    )

    def read_and_cancel() -> np.ndarray:
        listener.cancel()
        return chunk1

    fake_source.read = read_and_cancel  # cancel() implies running=False too

    results = list(listener.capture_until_stop())

    assert results == []
    assert fake_source.stopped is True


def test_listen_vad_loop_yields_one_utterance_via_fake_source(monkeypatch):
    """Drive Listener.listen()'s VAD/endpointing loop end-to-end with a fake
    source and a monkeypatched _is_speech, with no webrtcvad/hardware
    involved. min_speech_frames=13 (400ms/30ms), silence_frames_needed=26
    (800ms/30ms) at the project's default config.
    """
    frame_size = 480  # SAMPLE_RATE(16000) * FRAME_MS(30) / 1000
    speech_frames = 13
    silence_frames = 26
    total_frames = speech_frames + silence_frames
    big_chunk = np.zeros(frame_size * total_frames, dtype=np.float32)

    fake_source = FakeAudioSource(chunks=[big_chunk])

    listener = Listener(
        FakeTranscriber(text="hello listen"),
        open_source=lambda device_index: fake_source,
    )
    fake_source.listener = listener

    call_count = {"n": 0}

    def fake_is_speech(frame: np.ndarray) -> bool:
        call_count["n"] += 1
        return call_count["n"] <= speech_frames

    monkeypatch.setattr(listener, "_is_speech", fake_is_speech)

    results = list(listener.listen())

    assert results == ["hello listen"]
    assert fake_source.started is True
    assert fake_source.stopped is True


def test_a_fresh_source_is_opened_for_every_run(monkeypatch):
    """The factory must be called per run, never hoisted into __init__.

    Each capture method opens its own source and stops it in a finally, so
    reusing one instance across runs would leave the second run reading from
    an already-stopped device. This is the guarantee the seam has to keep.
    """
    opened: list[FakeAudioSource] = []
    holder: dict[str, Listener] = {}

    def factory(device_index):
        source = FakeAudioSource([np.zeros(320, dtype=np.float32)])
        source.listener = holder["listener"]
        opened.append(source)
        return source

    listener = Listener(FakeTranscriber(text="run"), open_source=factory)
    holder["listener"] = listener
    monkeypatch.setattr(listener, "_is_speech", lambda frame: False)

    listener.running = True
    list(listener.listen())
    listener.running = True
    list(listener.listen())

    assert len(opened) == 2, "each run must open its own source"
    assert all(source.stopped for source in opened), "every source must be stopped"
