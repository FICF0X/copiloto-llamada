"""Speech-to-text using faster-whisper. Runs on GPU (CUDA) when available, else CPU."""
from __future__ import annotations

from src import cuda_setup  # noqa: F401 - registers CUDA DLLs, MUST come before faster_whisper

import numpy as np
from faster_whisper import WhisperModel

from src.config import WHISPER_LANGUAGE, WHISPER_MODEL


class Transcriber:
    """Wraps a Whisper model and turns audio buffers into text."""

    def __init__(
        self,
        model_size: str = WHISPER_MODEL,
        language: str | None = WHISPER_LANGUAGE,
    ) -> None:
        self.language = language
        self.last_language: str = ""  # language detected on the last transcription
        self.model, self.device, self.compute_type = self._load_model(model_size)

    def _load_model(self, model_size: str) -> tuple[WhisperModel, str, str]:
        """Try GPU first (float16), fall back to CPU (int8)."""
        try:
            model = WhisperModel(model_size, device="cuda", compute_type="float16")
            return model, "cuda", "float16"
        except Exception as exc:  # noqa: BLE001 - any CUDA/driver error -> CPU
            print(f"[transcriber] GPU not available, using CPU. Reason: {exc}")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            return model, "cpu", "int8"

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a mono float32 @ 16 kHz buffer into a single string."""
        if audio.size == 0:
            return ""
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1,  # greedy = faster, fine for short utterances
            vad_filter=True,  # drop leading/trailing silence inside the buffer
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        self.last_language = getattr(info, "language", "") or ""
        return text


def _test() -> None:
    """Capture a few seconds of speech and transcribe it."""
    from src.audio_capture import SystemAudioCapture

    print("Loading Whisper model (downloads ~460 MB the first time)...")
    tr = Transcriber()
    print(f"[OK] Model loaded on: {tr.device} ({tr.compute_type})\n")

    seconds = 6
    print(f"Recording {seconds}s. PLAY a video/audio with SPEECH now...\n")
    with SystemAudioCapture() as cap:
        chunks = [cap.read() for _ in range(int(seconds * 1000 / cap.chunk_ms))]
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    print("Transcribing...\n")
    text = tr.transcribe(audio)
    print("-" * 50)
    print(f"DETECTED LANGUAGE: {tr.last_language or '(none)'}")
    print(f"TRANSCRIPTION: {text or '(nothing detected)'}")
    print("-" * 50)


if __name__ == "__main__":
    _test()
