"""Live listener: continuously captures system audio, detects when a speaker
finishes an utterance (endpointing via VAD), and transcribes that utterance.

This is the piece that turns raw audio into discrete questions ready for the AI.
"""
from __future__ import annotations

import threading
from typing import Callable, Iterator

import numpy as np
import webrtcvad

from src import audio_source
from src.audio_source import SystemAudioSource
from src.config import (
    FRAME_MS,
    MAX_UTTERANCE_S,
    PARTIAL_JOIN_TIMEOUT_S,
    PARTIAL_WINDOW_S,
    SAMPLE_RATE,
    SILENCE_MS_TO_ENDPOINT,
)
from src.transcriber import Transcriber

# How often the live-transcription thread refreshes the partial text (seconds).
PARTIAL_INTERVAL_S = 1.0


def _tail(frames: list[np.ndarray], max_samples: int) -> np.ndarray:
    """Concatenate only the LAST max_samples worth of audio from frames.

    Copying just the tail is what keeps the live preview cheap: cost depends on
    the window, not on how long the recording has been running.
    """
    picked: list[np.ndarray] = []
    total = 0
    for frame in reversed(frames):
        picked.append(frame)
        total += frame.size
        if total >= max_samples:
            break
    if not picked:
        return np.zeros(0, dtype=np.float32)
    picked.reverse()
    audio = np.concatenate(picked)
    return audio[-max_samples:] if audio.size > max_samples else audio


class Listener:
    """Yields transcribed utterances as the speaker pauses."""

    def __init__(
        self,
        transcriber: Transcriber,
        aggressiveness: int = 2,  # 0..3, higher = filters more non-speech
        min_speech_ms: int = 400,  # ignore blips shorter than this
        device_index: int | None = None,  # which loopback device to capture
        open_source: Callable[[int | None], SystemAudioSource] = audio_source.open_source,
    ) -> None:
        self.vad = webrtcvad.Vad(aggressiveness)
        self.transcriber = transcriber
        self.device_index = device_index
        # Injected factory, not an instance: each capture method builds a fresh
        # source and stop()s it in `finally`, so the source is per-RUN. A single
        # injected instance couldn't be restarted across two listen() calls
        # without inventing lifecycle rules that don't exist today.
        self._open_source = open_source
        self.frame_size = int(SAMPLE_RATE * FRAME_MS / 1000)  # samples per VAD frame
        self.silence_frames_needed = SILENCE_MS_TO_ENDPOINT // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self.partial_window_samples = int(SAMPLE_RATE * PARTIAL_WINDOW_S)
        self.max_utterance_frames = int(MAX_UTTERANCE_S * 1000) // FRAME_MS
        self.running = False
        self.cancelled = False
        # Serializes ALL model use so the live-transcription thread and the final
        # transcription never hit Whisper at the same time.
        self._model_lock = threading.Lock()

    def _transcribe_locked(self, audio: np.ndarray) -> str:
        with self._model_lock:
            return self.transcriber.transcribe(audio)

    def _transcribe_preview(self, audio: np.ndarray) -> str:
        """Transcribe for the live preview, but only if the model is free.

        Never waits for the lock: the final transcription is what the user is
        actually waiting on, so it always wins the model. A skipped preview just
        costs one missed refresh, which nobody notices.
        """
        if not self._model_lock.acquire(blocking=False):
            return ""
        try:
            return self.transcriber.transcribe(audio)
        finally:
            self._model_lock.release()

    def stop(self) -> None:
        """Signal listen() to stop after the current chunk (~100 ms latency)."""
        self.running = False

    def cancel(self) -> None:
        """Abandon the capture without transcribing it.

        Stop means "I am done talking, answer me"; cancel means "forget it".
        Skipping the final transcription is what makes cancelling feel instant
        instead of making the user wait out the work they just called off.
        """
        self.cancelled = True
        self.running = False

    def capture_until_stop(
        self,
        on_state: Callable[[str], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
    ) -> Iterator[str]:
        """Controlled mode: record ALL audio from start until stop() is called,
        then transcribe the whole thing as a single utterance and yield it once.

        No VAD endpointing here on purpose: silence-based endpointing can't tell a
        "I'm done" pause from an "I'm thinking" pause, so it cuts the speaker off
        mid-thought. Letting the user press Stop puts that decision where the
        semantic knowledge actually lives — with the human.

        on_partial, if given, receives the live transcription of the last
        PARTIAL_WINDOW_S seconds heard, refreshed on a background thread so
        capture never stalls. It is a "still hearing you" indicator, not the
        transcript — the full recording is transcribed once, at the end.
        """
        def state(value: str) -> None:
            if on_state is not None:
                on_state(value)

        cap = self._open_source(self.device_index)
        cap.start()
        self.running = True
        self.cancelled = False

        frames: list[np.ndarray] = []
        frames_lock = threading.Lock()
        stop_partial = threading.Event()

        def partial_loop() -> None:
            last_len = 0
            while not stop_partial.wait(PARTIAL_INTERVAL_S):
                with frames_lock:
                    n = len(frames)
                    snap = _tail(frames, self.partial_window_samples) if n > last_len else None
                if snap is None:
                    continue
                last_len = n
                text = self._transcribe_preview(snap)
                # Drop the result if capture stopped meanwhile: from that point
                # on the final transcription owns what the user sees.
                if text and not stop_partial.is_set() and on_partial is not None:
                    on_partial(text)

        worker = None
        if on_partial is not None:
            worker = threading.Thread(target=partial_loop, daemon=True)
            worker.start()

        state("capturing")
        try:
            while self.running:
                chunk = cap.read()  # read() blocks per chunk, no busy-loop
                with frames_lock:
                    frames.append(chunk)
        finally:
            stop_partial.set()
            if worker is not None:
                # Bounded: a preview pass is capped by the window, but if one
                # ever hangs it must not hold the answer hostage. The thread is
                # a daemon and its late result is discarded anyway.
                worker.join(PARTIAL_JOIN_TIMEOUT_S)
            cap.stop()

        if not frames or self.cancelled:
            return
        state("transcribing")
        audio = np.concatenate(frames)
        text = self._transcribe_locked(audio)
        if text:
            yield text

    def _is_speech(self, frame: np.ndarray) -> bool:
        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return self.vad.is_speech(pcm16, SAMPLE_RATE)

    def listen(
        self,
        on_state: Callable[[str], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
    ) -> Iterator[str]:
        """Yield transcribed utterances.

        on_state, if given, is called with live capture states for UI feedback:
        "idle" (waiting for speech), "speech" (someone is talking),
        "transcribing" (running Whisper on the finished utterance).

        on_partial, if given, receives the live transcription of the last
        PARTIAL_WINDOW_S seconds of the CURRENT utterance, refreshed on a
        background thread so the VAD loop never stalls waiting on Whisper.
        """
        def state(value: str) -> None:
            if on_state is not None:
                on_state(value)

        cap = self._open_source(self.device_index)
        cap.start()
        self.running = True
        self.cancelled = False

        leftover = np.zeros(0, dtype=np.float32)  # samples not yet framed
        # Mutated in place (never reassigned) so the partial thread's reference
        # stays valid across utterances; reset is utterance.clear().
        utterance: list[np.ndarray] = []
        utt_lock = threading.Lock()
        stop_partial = threading.Event()
        speech_frames = 0
        silence_frames = 0
        in_speech = False
        state("idle")

        def partial_loop() -> None:
            last_len = 0
            while not stop_partial.wait(PARTIAL_INTERVAL_S):
                with utt_lock:
                    n = len(utterance)
                    if n < last_len:  # utterance was reset -> a new one started
                        last_len = 0
                    snap = (
                        _tail(utterance, self.partial_window_samples)
                        if n > last_len and n
                        else None
                    )
                if snap is None:
                    continue
                last_len = n
                text = self._transcribe_preview(snap)
                if text and not stop_partial.is_set() and on_partial is not None:
                    on_partial(text)

        worker = None
        if on_partial is not None:
            worker = threading.Thread(target=partial_loop, daemon=True)
            worker.start()

        try:
            while self.running:
                chunk = cap.read()
                leftover = np.concatenate([leftover, chunk])

                while leftover.size >= self.frame_size:
                    frame = leftover[: self.frame_size]
                    leftover = leftover[self.frame_size :]

                    if self._is_speech(frame):
                        if not in_speech:
                            state("speech")  # someone just started talking
                        with utt_lock:
                            utterance.append(frame)
                            utt_frames = len(utterance)
                        speech_frames += 1
                        silence_frames = 0
                        in_speech = True
                    elif in_speech:
                        with utt_lock:
                            utterance.append(frame)  # keep trailing silence for context
                            utt_frames = len(utterance)
                        silence_frames += 1
                    else:
                        continue  # silence outside an utterance: nothing to close

                    # Close the utterance on a real pause, or when it has run so
                    # long that waiting for a pause would only keep growing the
                    # buffer — and the cost of transcribing it — without bound.
                    paused = silence_frames >= self.silence_frames_needed
                    too_long = utt_frames >= self.max_utterance_frames
                    if not (paused or too_long):
                        continue

                    if speech_frames >= self.min_speech_frames and not self.cancelled:
                        state("transcribing")
                        with utt_lock:
                            audio = np.concatenate(utterance)
                        text = self._transcribe_locked(audio)
                        if text:
                            yield text
                    # reset for the next utterance (in place, keeps ref)
                    with utt_lock:
                        utterance.clear()
                    speech_frames = 0
                    silence_frames = 0
                    in_speech = False
                    state("idle")
        finally:
            stop_partial.set()
            if worker is not None:
                worker.join(PARTIAL_JOIN_TIMEOUT_S)
            cap.stop()


def _test(seconds: int = 30) -> None:
    """Listen for a while and print each detected utterance live."""
    import time

    print("Loading model...")
    listener = Listener(Transcriber())
    print(f"[OK] Listening for {seconds}s. Play speech with PAUSES between sentences.\n")
    print("Each time the speaker pauses, the utterance appears below:\n")

    start = time.time()
    count = 0
    for text in listener.listen():
        count += 1
        print(f"  [{count}] {text}")
        if time.time() - start > seconds:
            break

    print(f"\nDone. Detected {count} utterance(s).")


if __name__ == "__main__":
    _test()
