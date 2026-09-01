"""Tests for src/language_lock.py. Pure - no faster-whisper, no Qt, no
argostranslate involved anywhere in this file."""
from __future__ import annotations

from src.language_lock import LanguageLock


def test_below_threshold_is_ignored_and_does_not_lock():
    lock = LanguageLock(min_probability=0.70, min_votes=2)

    result = lock.observe("es", 0.40)

    assert result is None
    assert lock.locked == ""


def test_below_threshold_does_not_break_an_existing_streak():
    """A single ambiguous sample between two confident agreeing ones must not
    reset the streak - it is ignored, not treated as disagreement."""
    lock = LanguageLock(min_probability=0.70, min_votes=2)

    assert lock.observe("es", 0.90) is None
    assert lock.observe("fr", 0.30) is None  # low-confidence noise, ignored
    result = lock.observe("es", 0.85)  # second confident "es" -> locks

    assert result == "es"
    assert lock.locked == "es"


def test_two_consecutive_confident_agreeing_samples_lock():
    lock = LanguageLock(min_probability=0.70, min_votes=2)

    assert lock.observe("fr", 0.80) is None
    result = lock.observe("fr", 0.75)

    assert result == "fr"
    assert lock.locked == "fr"


def test_disagreement_resets_the_streak_to_the_new_candidate():
    lock = LanguageLock(min_probability=0.70, min_votes=2)

    assert lock.observe("es", 0.90) is None
    assert lock.observe("de", 0.85) is None  # disagrees -> streak resets to 1
    assert lock.locked == ""
    result = lock.observe("de", 0.80)  # second consecutive "de" -> locks

    assert result == "de"
    assert lock.locked == "de"


def test_once_locked_further_observations_are_a_frozen_noop():
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    lock.observe("es", 0.90)
    lock.observe("es", 0.90)
    assert lock.locked == "es"

    result = lock.observe("de", 0.99)  # loud, confident, irrelevant now

    assert result is None
    assert lock.locked == "es"


def test_max_attempts_fallback_locks_to_most_frequent_candidate():
    """Never reaching min_votes consecutive agreement must not leave the
    session caption-less forever: after max_attempts detections, lock to
    whichever language was seen most often overall."""
    lock = LanguageLock(min_probability=0.70, min_votes=2, max_attempts=6)

    # Alternating so no two CONSECUTIVE samples ever agree, but "es" is the
    # most frequent overall (4 of 6).
    sequence = [("es", 0.90), ("en", 0.90), ("es", 0.90), ("en", 0.90), ("es", 0.90), ("es", 0.90)]
    results = [lock.observe(lang, prob) for lang, prob in sequence]

    assert results[:-1] == [None, None, None, None, None]
    assert results[-1] == "es"
    assert lock.locked == "es"


def test_max_attempts_only_counts_utterances_that_produced_a_detection():
    """An utterance with no detection at all (empty language) must not count
    toward max_attempts - staying unlocked forever is correct if nothing was
    ever detected, per the design."""
    lock = LanguageLock(min_probability=0.70, min_votes=2, max_attempts=3)

    for _ in range(10):
        assert lock.observe("", 0.0) is None

    assert lock.locked == ""


def test_override_wins_immediately_and_sets_manual_flag():
    lock = LanguageLock()

    lock.override("de")

    assert lock.locked == "de"
    assert lock.manual is True


def test_override_wins_permanently_and_freezes_observation():
    """Once a human has spoken, the detector never argues back: observe()
    must not just fail to relock, it must not mutate any internal counters
    either."""
    lock = LanguageLock(min_probability=0.70, min_votes=2)
    lock.override("de")

    for _ in range(20):
        result = lock.observe("fr", 0.99)
        assert result is None
        assert lock.locked == "de"  # never budges


def test_override_before_any_detection_also_wins():
    lock = LanguageLock()

    lock.override("pt")
    result = lock.observe("es", 0.99)

    assert result is None
    assert lock.locked == "pt"


def test_reset_clears_lock_manual_and_internal_counters():
    lock = LanguageLock(min_probability=0.70, min_votes=2, max_attempts=3)
    lock.override("de")

    lock.reset()

    assert lock.locked == ""
    assert lock.manual is False
    # After reset, detection must work again from scratch (proves internal
    # counters were cleared, not just the public fields).
    assert lock.observe("es", 0.90) is None
    assert lock.observe("es", 0.90) == "es"


def test_behavior_while_unlocked_returns_none_without_side_effects_on_locked_field():
    lock = LanguageLock(min_probability=0.70, min_votes=3)

    result = lock.observe("es", 0.95)

    assert result is None
    assert lock.locked == ""
    assert lock.manual is False


def test_unsure_guesses_never_outvote_the_one_confident_sample():
    """A handful of low-confidence guesses must not decide the session.

    Counting them as votes let six sub-threshold guesses beat the single
    confident detection, locking the call to the wrong language, downloading
    the wrong package and mistranscribing everything said afterwards.
    """
    lock = LanguageLock(min_probability=0.70, min_votes=2, max_attempts=6)
    samples = [
        ("es", 0.95),
        ("it", 0.30),
        ("pt", 0.31),
        ("it", 0.32),
        ("pt", 0.33),
        ("it", 0.34),
    ]
    for language, probability in samples:
        lock.observe(language, probability)

    assert lock.locked == "es"


def test_the_fallback_locks_to_the_most_confident_language_seen():
    """Nobody may be left without captions forever.

    After max_attempts without two consecutive agreeing samples, the lock
    settles on the language seen most often among the confident ones.
    """
    lock = LanguageLock(min_probability=0.70, min_votes=3, max_attempts=4)
    for language in ("es", "en", "es", "en"):
        lock.observe(language, 0.90)

    assert lock.locked in ("es", "en")
    assert lock.observe("fr", 0.99) is None, "a locked session stops observing"


def test_the_fallback_stays_silent_when_nothing_was_ever_confident():
    """Guessing from noise is worse than waiting: no confident sample, no lock."""
    lock = LanguageLock(min_probability=0.70, min_votes=2, max_attempts=3)
    for _ in range(5):
        lock.observe("it", 0.20)

    assert lock.locked == ""
