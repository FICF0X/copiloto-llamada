"""Live listener: continuously captures system audio, detects when a speaker
finishes an utterance (endpointing via VAD), and transcribes that utterance.

This is the piece that turns raw audio into discrete questions ready for the AI.
"""
from __future__ import annotations

from typing import Iterator

import numpy as np
import webrtcvad

from src.audio_capture import SystemAudioCapture
from src.config import FRAME_MS, SAMPLE_RATE, SILENCE_MS_TO_ENDPOINT
from src.transcriber import Transcriber


class Listener:
    """Yields transcribed utterances as the speaker pauses."""

    def __init__(
        self,
        transcriber: Transcriber,
        aggressiveness: int = 2,  # 0..3, higher = filters more non-speech
        min_speech_ms: int = 400,  # ignore blips shorter than this
        device_index: int | None = None,  # which loopback device to capture
    ) -> None:
        self.vad = webrtcvad.Vad(aggressiveness)
        self.transcriber = transcriber
        self.device_index = device_index
        self.frame_size = int(SAMPLE_RATE * FRAME_MS / 1000)  # samples per VAD frame
        self.silence_frames_needed = SILENCE_MS_TO_ENDPOINT // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self.running = False

    def stop(self) -> None:
        """Signal listen() to stop after the current chunk (~100 ms latency)."""
        self.running = False

    def _is_speech(self, frame: np.ndarray) -> bool:
        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return self.vad.is_speech(pcm16, SAMPLE_RATE)

    def listen(self) -> Iterator[str]:
        cap = SystemAudioCapture(device_index=self.device_index)
        cap.start()
        self.running = True

        leftover = np.zeros(0, dtype=np.float32)  # samples not yet framed
        utterance: list[np.ndarray] = []
        speech_frames = 0
        silence_frames = 0
        in_speech = False

        try:
            while self.running:
                chunk = cap.read()
                leftover = np.concatenate([leftover, chunk])

                while leftover.size >= self.frame_size:
                    frame = leftover[: self.frame_size]
                    leftover = leftover[self.frame_size :]

                    if self._is_speech(frame):
                        utterance.append(frame)
                        speech_frames += 1
                        silence_frames = 0
                        in_speech = True
                    elif in_speech:
                        utterance.append(frame)  # keep trailing silence for context
                        silence_frames += 1

                        # Enough silence after real speech -> utterance ended.
                        if silence_frames >= self.silence_frames_needed:
                            if speech_frames >= self.min_speech_frames:
                                audio = np.concatenate(utterance)
                                text = self.transcriber.transcribe(audio)
                                if text:
                                    yield text
                            # reset for the next utterance
                            utterance = []
                            speech_frames = 0
                            silence_frames = 0
                            in_speech = False
        finally:
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
