"""Tracks how many real Gemini requests were made today.

This is an ESTIMATE, not Google's official number. Gemini's API does not expose
the remaining quota, so the closest honest gauge is to count each actual API
call the app makes (one per answer). It resets at local midnight and is persisted
to disk so it survives app restarts within the same day.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path


class UsageTracker:
    """Counts real API requests per day, backed by a small text file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> tuple[str, int]:
        try:
            day, count = self._path.read_text(encoding="utf-8").strip().split(",", 1)
            return day, int(count)
        except (OSError, ValueError):
            return "", 0

    def today_count(self) -> int:
        """Requests counted for today (0 if the stored day is not today)."""
        day, count = self._read()
        return count if day == date.today().isoformat() else 0

    def record(self) -> int:
        """Count one real request and return the new total for today."""
        today = date.today().isoformat()
        day, count = self._read()
        count = count + 1 if day == today else 1
        try:
            self._path.write_text(f"{today},{count}", encoding="utf-8")
        except OSError:
            pass
        return count
