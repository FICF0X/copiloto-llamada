"""Centralized configuration. Loads secrets from the .env file."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    # Running as a PyInstaller build: __file__ points into the bundle (a temp
    # extraction dir, or the onedir app folder), which can be read-only or
    # wiped between runs. Settings/.env must live somewhere that survives and
    # is writable, so use the folder the .exe itself sits in.
    ROOT = Path(sys.executable).resolve().parent
    # Bundled read-only resources (e.g. assets/icon.ico) are NOT next to the
    # .exe in onedir builds (they live under _internal/) — PyInstaller always
    # points sys._MEIPASS at wherever it actually put them, for both onedir
    # and onefile, so resource lookups must use this instead of ROOT.
    RESOURCES_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    # Project root = parent of the src/ folder.
    ROOT = Path(__file__).resolve().parent.parent
    RESOURCES_ROOT = ROOT

# Load variables from .env into the environment.
load_dotenv(ROOT / ".env")

# --- Where remembered state lives ---
# Small plain files rather than a settings store: they are easy to inspect, easy
# to delete when something goes wrong, and survive the app being rewritten.
CONTEXT_FILE = ROOT / "context.txt"  # the meeting prompt
DEVICE_FILE = ROOT / "audio_device.txt"  # last chosen loopback device
USAGE_FILE = ROOT / "usage.txt"  # estimated requests sent today
MODE_FILE = ROOT / "listen_mode.txt"  # "auto" or "controlled"
LENGTH_FILE = ROOT / "answer_length.txt"  # "short" or "detailed"
HIDE_FILE = ROOT / "hide_from_screenshare.txt"  # "1" or "0", user toggle
CONVERSATIONS_DIR = ROOT / "conversations"  # one JSON per saved call

# --- Privacy ---
# Default for the first run only (no HIDE_FILE saved yet). After that, the
# "Ocultar al compartir pantalla" toggle in the chat window and HIDE_FILE are
# the source of truth. True hides the windows from screen capture and screen
# sharing while leaving them visible locally. Requires Windows 10 2004+.
HIDE_FROM_SCREENSHARE: bool = False

# --- Secrets ---
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# --- Gemini model ---
# Flash Lite 3.1: free tier gives 500 requests/day (vs 20 for others) + 15 RPM.
GEMINI_MODEL: str = "gemini-3.1-flash-lite"
# Milliseconds the API may go silent before the request is aborted. Without a
# timeout a stalled stream blocks the worker thread, and the UI with it, forever.
GEMINI_TIMEOUT_MS: int = 60_000

# --- Audio ---
SAMPLE_RATE: int = 16_000  # Whisper expects 16 kHz mono.
CHANNELS: int = 1
FRAME_MS: int = 30  # VAD works on 10/20/30 ms frames.

# --- Transcription ---
# Model size: tiny < base < small < medium < large. Bigger = more accurate, slower.
# "small" is the sweet spot for the RTX 3050 in real time.
WHISPER_MODEL: str = "small"
# Locked to English for accuracy + speed (no language guessing).
# Set None to auto-detect, or "es" for Spanish.
WHISPER_LANGUAGE: str | None = "en"

# How many ms of silence marks the end of a question (endpointing).
SILENCE_MS_TO_ENDPOINT: int = 800

# --- Live transcription (the preview shown while someone is still talking) ---
# The preview transcribes only the most recent slice of audio, never the whole
# recording. Without this cap each refresh re-transcribes an ever-growing buffer,
# so the work per refresh grows with the recording until Whisper can no longer
# keep up and the app appears frozen.
# 15 s measured at ~0.7 s per pass on an RTX 3050; 20 s jumps to ~2 s because the
# clip stops fitting Whisper's internal 30 s window cleanly.
PARTIAL_WINDOW_S: float = 15.0
# How long to wait for an in-flight preview pass when capture stops, before
# giving up on it and going straight to the final transcription.
PARTIAL_JOIN_TIMEOUT_S: float = 5.0
# Auto mode: force an endpoint when someone has talked this long without a real
# pause, so a speaker who never pauses cannot grow a single utterance without
# bound (and with it, the cost of every transcription pass over it).
MAX_UTTERANCE_S: float = 45.0

# --- Translator: source-language detect-and-lock (src/language_lock.py) ---
# All three are unvalidated STARTING values pending the owner's calibration
# against real call audio (slice 6 manual checklist) - kept here, not
# hardcoded in language_lock.py, so tuning is a one-line change.
# language_probability is a softmax over ~100 languages; >0.70 is meant to be
# a decisive margin while <0.70 is usually a confusable-language cluster
# (e.g. es/pt/it) on a short VAD-endpointed clip.
LANGUAGE_LOCK_MIN_PROBABILITY: float = 0.70
# Consecutive (not majority) agreeing confident samples needed to lock.
LANGUAGE_LOCK_MIN_VOTES: int = 2
# After this many utterances that produced ANY detection, lock to whichever
# language was seen most often rather than leaving the session caption-less.
LANGUAGE_LOCK_MAX_ATTEMPTS: int = 6

# --- Conversation memory ---
# Past messages (questions + answers) kept in context so follow-up questions
# like "about what you just mentioned..." make sense. Higher = better memory but
# more tokens per request. 16 = roughly the last 8 question/answer exchanges.
MAX_HISTORY_MESSAGES: int = 16


def validate() -> None:
    """Fail fast with a clear message if the API key is missing."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is empty. Create a .env file in the project root "
            "with: GEMINI_API_KEY=your_key"
        )


def set_api_key(key: str) -> None:
    """Save a Gemini API key entered at runtime (first-run prompt in the UI).

    Updates this module's GEMINI_API_KEY (so `validate()` and anything reading
    `config.GEMINI_API_KEY` see it immediately), the process environment (so a
    plain os.getenv also sees it), and writes/updates .env next to ROOT so the
    key survives the next launch too — the standalone build has no run.bat to
    ask for it again.
    """
    global GEMINI_API_KEY
    GEMINI_API_KEY = key
    os.environ["GEMINI_API_KEY"] = key

    try:
        env_path = ROOT / ".env"
        lines = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.exists()
            else []
        )
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("GEMINI_API_KEY="):
                lines[i] = f"GEMINI_API_KEY={key}"
                found = True
                break
        if not found:
            lines.append(f"GEMINI_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        # Key still works for this session (env var + GEMINI_API_KEY above);
        # it just won't survive a restart if ROOT isn't writable.
        pass
