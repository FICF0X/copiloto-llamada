"""Capture system audio (whatever is playing through the speakers) via WASAPI loopback.

Whisper needs 16 kHz mono float32 audio, but sound cards play at 44.1/48 kHz stereo.
This module captures at the device's native format and converts on the fly.
"""
from __future__ import annotations

from typing import Iterator

import numpy as np
import pyaudiowpatch as pyaudio

from src.config import SAMPLE_RATE


def list_loopback_devices() -> list[dict]:
    """List the available WASAPI loopback devices.

    Each entry is {"index", "name", "is_default"} where "is_default" marks the
    loopback of the current default speakers. Used to populate the UI selector.
    """
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        default_name = default_out["name"]

        devices: list[dict] = []
        for loopback in pa.get_loopback_device_info_generator():
            devices.append(
                {
                    "index": int(loopback["index"]),
                    "name": str(loopback["name"]),
                    "is_default": default_name in loopback["name"],
                }
            )
        return devices
    finally:
        pa.terminate()


class SystemAudioCapture:
    """Streams system audio as 16 kHz mono float32 chunks."""

    def __init__(
        self,
        target_rate: int = SAMPLE_RATE,
        chunk_ms: int = 100,
        device_index: int | None = None,
    ) -> None:
        self.target_rate = target_rate
        self.chunk_ms = chunk_ms
        # When set, capture this exact loopback device instead of the default one.
        self.device_index = device_index
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self.device_rate: int = 0
        self.channels: int = 0
        self.device_name: str = ""

    def _find_loopback_device(self, pa: pyaudio.PyAudio) -> dict:
        """Find the loopback of the current default speakers."""
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])

        # If the default output is already a loopback device, use it directly.
        if default_out.get("isLoopbackDevice", False):
            return default_out

        # Otherwise find the matching loopback by name.
        for loopback in pa.get_loopback_device_info_generator():
            if default_out["name"] in loopback["name"]:
                return loopback

        raise RuntimeError(
            "No WASAPI loopback device found. Make sure audio output is active."
        )

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()
        if self.device_index is not None:
            device = self._pa.get_device_info_by_index(self.device_index)
        else:
            device = self._find_loopback_device(self._pa)

        self.device_rate = int(device["defaultSampleRate"])
        self.channels = int(device["maxInputChannels"])
        self.device_name = device["name"]

        frames_per_buffer = int(self.device_rate * self.chunk_ms / 1000)
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.device_rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=frames_per_buffer,
        )

    def _to_mono_16k(self, raw: bytes) -> np.ndarray:
        """int16 interleaved bytes -> mono float32 resampled to target_rate."""
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        # Downmix to mono by averaging channels.
        if self.channels > 1:
            audio = audio.reshape(-1, self.channels).mean(axis=1)

        # Resample to 16 kHz with linear interpolation (good enough for speech).
        if self.device_rate != self.target_rate and audio.size:
            n_out = int(round(audio.size * self.target_rate / self.device_rate))
            x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)

        return audio

    def read(self) -> np.ndarray:
        """Read one chunk as mono float32 @ target_rate."""
        if self._stream is None:
            raise RuntimeError("Capture not started. Call start() first.")
        frames = int(self.device_rate * self.chunk_ms / 1000)
        raw = self._stream.read(frames, exception_on_overflow=False)
        return self._to_mono_16k(raw)

    def stream(self) -> Iterator[np.ndarray]:
        """Yield audio chunks continuously until stopped."""
        while True:
            yield self.read()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def __enter__(self) -> "SystemAudioCapture":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def _level_test(seconds: int = 5) -> None:
    """Capture for a few seconds and report the audio level.

    Play music or a video while this runs to confirm system audio is captured.
    """
    print(f"Capturing system audio for {seconds}s... PLAY some audio now!\n")
    with SystemAudioCapture() as cap:
        print(f"Device: {cap.device_name}")
        print(f"Native rate: {cap.device_rate} Hz, channels: {cap.channels}")
        print(f"Output rate: {cap.target_rate} Hz mono\n")

        chunks_needed = int(seconds * 1000 / cap.chunk_ms)
        peak = 0.0
        collected = []
        for i in range(chunks_needed):
            chunk = cap.read()
            collected.append(chunk)
            rms = float(np.sqrt(np.mean(chunk**2))) if chunk.size else 0.0
            peak = max(peak, rms)
            bar = "#" * int(rms * 80)
            print(f"  chunk {i + 1:>3}/{chunks_needed}  level |{bar:<40}|", end="\r")

        print("\n")
        full = np.concatenate(collected) if collected else np.zeros(0)
        if peak < 0.001:
            print("[!] Almost no sound detected. Was audio playing?")
        else:
            print(f"[OK] Audio captured! peak level: {peak:.4f}, samples: {full.size}")


if __name__ == "__main__":
    _level_test()
