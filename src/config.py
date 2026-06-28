"""Centralized configuration. Loads secrets from the .env file."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the src/ folder.
ROOT = Path(__file__).resolve().parent.parent

# Load variables from .env into the environment.
load_dotenv(ROOT / ".env")

# --- Secrets ---
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# --- Gemini model ---
# Flash Lite 3.1: free tier gives 500 requests/day (vs 20 for others) + 15 RPM.
GEMINI_MODEL: str = "gemini-3.1-flash-lite"

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
