"""Detect-and-lock the Translator engine's source language.

Pure, zero imports beyond dataclasses/typing - no faster-whisper, no Qt, no
argostranslate. TranslatorStrategy is the only consumer.

Why this exists: language_probability is a softmax over ~100 languages
computed on a single short (400ms-a few seconds) VAD-endpointed utterance.
A sub-second clip routinely yields a confident-*looking* wrong guess, so
translating on every utterance's raw detection would flip-flop and could
even trigger downloading a package for the wrong language pair. Locking
once, from a few agreeing confident samples, trades a short "detecting..."
window at the start of a session for a stable session-long source language.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LanguageLock:
    """Session-scoped source-language decision for Translator mode.

    Tunables (min_probability/min_votes/max_attempts) are unvalidated
    starting values - see src.config.LANGUAGE_LOCK_* - and are meant to be
    calibrated by the owner against real call audio (slice 6 manual task).
    """

    min_probability: float = 0.70
    min_votes: int = 2
    max_attempts: int = 6
    locked: str = ""
    manual: bool = False

    # Internal bookkeeping only - never read/written from outside this class.
    _candidate: str = field(default="", repr=False, compare=False)
    _streak: int = field(default=0, repr=False, compare=False)
    _attempts: int = field(default=0, repr=False, compare=False)
    _votes: dict = field(default_factory=dict, repr=False, compare=False)

    def observe(self, language: str, probability: float) -> str | None:
        """Feed one utterance's detection in. Returns the language code at
        the exact moment locking happens, else None.

        A manual override or an existing lock makes this an immediate no-op
        that mutates NOTHING - "override wins permanently" and "already
        locked" both mean observation is frozen, not just ignored once.
        """
        if self.manual or self.locked:
            return None
        if not language:
            # No detection at all (e.g. silence/empty audio): doesn't count
            # as an "attempt" per max_attempts' own definition below.
            return None

        self._attempts += 1

        if probability >= self.min_probability:
            # Only a confident detection is a vote. Counting the unsure ones
            # let a handful of sub-threshold guesses outvote the single
            # confident sample and lock the whole session to the wrong
            # language.
            self._votes[language] = self._votes.get(language, 0) + 1
            if language == self._candidate:
                self._streak += 1
            else:
                # Disagreement: a new confident candidate resets the streak
                # to 1, it does not average against the old one.
                self._candidate = language
                self._streak = 1
            if self._streak >= self.min_votes:
                self.locked = language
                return language
        # A below-threshold sample is ambiguous, not a vote against the
        # current candidate: it counts toward max_attempts (a detection DID
        # happen) but never touches _candidate/_streak.

        if self._attempts >= self.max_attempts and self._votes:
            # The user must never be left caption-less forever: lock to
            # whichever language was seen most often, even without ever
            # reaching min_votes consecutive agreements.
            best = max(self._votes, key=self._votes.get)
            self.locked = best
            return best
        return None

    def override(self, code: str) -> None:
        """A human picked a language explicitly. Wins permanently: after
        this, observe() is a frozen no-op until reset()."""
        self.locked = code
        self.manual = True

    def reset(self) -> None:
        """Start a fresh session. Called by the owner of this lock (chat_app's
        _start_listening) - a new capture session, never mid-session."""
        self.locked = ""
        self.manual = False
        self._candidate = ""
        self._streak = 0
        self._attempts = 0
        self._votes = {}
