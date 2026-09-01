"""Tests for src/translator.py: route resolution, off-thread install, cache.

Fakes stand in for argostranslate's Language/Translation objects (the
pre-existing `_has_pair` seam) and for argostranslate.package's module
functions (monkeypatched directly, since Translator imports them lazily
inside its methods). No real argostranslate network/model calls here except
in the dedicated resolve_route() investigation tests, which run against the
REAL installed argostranslate 1.11.0 library.
"""
from __future__ import annotations

import pytest

from src.translator import (
    PackageIndexError,
    PackageInstallError,
    PackageUnavailable,
    Route,
    Translator,
    resolve_route,
)


class FakeTranslation:
    """A genuine single-hop translation (PackageTranslation/CachedTranslation
    stand-in): no t1/t2 chain attributes."""


class FakeComposite:
    """argostranslate's CompositeTranslation stand-in: has t1/t2, meaning the
    library synthesized this by chaining two other translations - it must
    NEVER be mistaken for a direct package."""

    def __init__(self):
        self.t1 = object()
        self.t2 = object()


class FakeLang:
    def __init__(self, code: str, translations: dict | None = None):
        self.code = code
        self._translations = translations or {}  # to_code -> translation|None

    def get_translation(self, other):
        return self._translations.get(other.code)


class FakePackage:
    def __init__(self, from_code: str, to_code: str, path="pkg.argosmodel"):
        self.from_code = from_code
        self.to_code = to_code
        self._path = path
        self.downloaded = False

    def download(self):
        self.downloaded = True
        return self._path


# --- resolve_route(): pure, direct/pivot/unavailable ------------------------


def test_resolve_route_direct_pair():
    es = FakeLang("es", {"en": FakeTranslation()})
    en = FakeLang("en", {"es": FakeTranslation()})

    route = resolve_route([es, en], "es", "en")

    assert route == Route(kind="direct", hops=("es", "en"))


def test_resolve_route_pivot_when_only_pivot_legs_exist():
    fr = FakeLang("fr", {"en": FakeTranslation()})
    en = FakeLang("en", {"fr": FakeTranslation(), "de": FakeTranslation()})
    de = FakeLang("de", {"en": FakeTranslation()})

    route = resolve_route([fr, en, de], "fr", "de")

    assert route == Route(kind="pivot", hops=("fr", "en", "de"))


def test_resolve_route_unavailable_when_nothing_connects():
    fr = FakeLang("fr", {})
    de = FakeLang("de", {})

    route = resolve_route([fr, de], "fr", "de")

    assert route == Route(kind="unavailable", hops=())


def test_resolve_route_unavailable_when_language_not_installed_at_all():
    en = FakeLang("en", {})

    route = resolve_route([en], "fr", "de")

    assert route == Route(kind="unavailable", hops=())


def test_resolve_route_never_trusts_a_composite_translation_as_direct():
    """Regression test for the investigation finding: argostranslate 1.11.0's
    get_installed_languages() pre-builds a CompositeTranslation for any pair
    reachable via a pivot and puts it in translations_from, so
    from_lang.get_translation(to_lang) returning non-None does NOT mean a
    genuine direct package exists. resolve_route must see through that and
    still label the route "pivot", not "direct" - the UI's pivot-quality
    warning must never be silently skipped.
    """
    es = FakeLang("es", {"en": FakeTranslation(), "de": FakeComposite()})
    en = FakeLang("en", {"de": FakeTranslation()})
    de = FakeLang("de", {})

    route = resolve_route([es, en, de], "es", "de")

    assert route.kind == "pivot"
    assert route.hops == ("es", "en", "de")


def test_resolve_route_pivot_legs_are_not_required_to_be_composite_free():
    """A pivot LEG only needs to exist - if it's itself composited from a
    further chain, translate() will still just call argostranslate for that
    hop and get a correct result. Only the outer direct-vs-pivot label cares
    about compositing."""
    fr = FakeLang("fr", {"en": FakeComposite()})  # a composited leg is fine
    en = FakeLang("en", {"fr": FakeTranslation(), "de": FakeTranslation()})
    de = FakeLang("de", {"en": FakeTranslation()})

    route = resolve_route([fr, en, de], "fr", "de")

    assert route.kind == "pivot"


def test_resolve_route_never_pivots_through_itself():
    en = FakeLang("en", {})
    de = FakeLang("de", {})

    route = resolve_route([en, de], "en", "de")

    assert route == Route(kind="unavailable", hops=())


# --- Translator.resolve(): per-instance cache --------------------------------


def test_resolve_caches_per_pair(monkeypatch):
    calls = {"n": 0}

    def fake_installed_languages(self):
        calls["n"] += 1
        return [FakeLang("es", {"en": FakeTranslation()}), FakeLang("en", {"es": FakeTranslation()})]

    monkeypatch.setattr(Translator, "_installed_languages", fake_installed_languages)
    tr = Translator()

    tr.resolve("es", "en")
    tr.resolve("es", "en")

    assert calls["n"] == 1


# --- Translator.ensure_route(): typed exceptions, blocking install ----------


def test_ensure_route_returns_immediately_when_already_available(monkeypatch):
    monkeypatch.setattr(
        Translator,
        "_installed_languages",
        lambda self: [FakeLang("es", {"en": FakeTranslation()}), FakeLang("en", {})],
    )
    import argostranslate.package as real_package

    def boom():
        raise AssertionError("update_package_index must not be called when already installed")

    monkeypatch.setattr(real_package, "update_package_index", boom)
    tr = Translator()

    route = tr.ensure_route("es", "en")

    assert route.kind == "direct"


def test_ensure_route_raises_package_index_error_on_index_failure(monkeypatch):
    monkeypatch.setattr(Translator, "_installed_languages", lambda self: [])
    import argostranslate.package as real_package

    def failing_update_index():
        raise RuntimeError("network down")

    monkeypatch.setattr(real_package, "update_package_index", failing_update_index)
    tr = Translator()

    with pytest.raises(PackageIndexError):
        tr.ensure_route("fr", "de")


def test_ensure_route_raises_package_unavailable_when_index_has_nothing(monkeypatch):
    monkeypatch.setattr(Translator, "_installed_languages", lambda self: [])
    import argostranslate.package as real_package

    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [])
    tr = Translator()

    with pytest.raises(PackageUnavailable):
        tr.ensure_route("xx", "yy")


def test_ensure_route_raises_package_install_error_when_download_or_install_fails(monkeypatch):
    monkeypatch.setattr(Translator, "_installed_languages", lambda self: [])
    import argostranslate.package as real_package

    pkg = FakePackage("es", "en")
    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [pkg])

    def failing_install(path):
        raise RuntimeError("disk full")

    monkeypatch.setattr(real_package, "install_from_path", failing_install)
    tr = Translator()

    with pytest.raises(PackageInstallError):
        tr.ensure_route("es", "en")
    assert pkg.downloaded is True


def test_ensure_route_installs_direct_package_and_reports_status(monkeypatch):
    calls = {"n": 0}

    def fake_installed_languages(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # nothing installed yet
        return [FakeLang("es", {"en": FakeTranslation()}), FakeLang("en", {})]

    monkeypatch.setattr(Translator, "_installed_languages", fake_installed_languages)
    import argostranslate.package as real_package

    pkg = FakePackage("es", "en")
    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [pkg])
    installed = []
    monkeypatch.setattr(real_package, "install_from_path", lambda path: installed.append(path))

    statuses = []
    tr = Translator()

    route = tr.ensure_route("es", "en", on_status=statuses.append)

    assert route.kind == "direct"
    assert pkg.downloaded is True
    assert installed == ["pkg.argosmodel"]
    assert any("Descargando" in s for s in statuses)


def test_ensure_route_installs_both_pivot_legs_when_no_direct_package(monkeypatch):
    calls = {"n": 0}

    def fake_installed_languages(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        fr = FakeLang("fr", {"en": FakeTranslation()})
        en = FakeLang("en", {"fr": FakeTranslation(), "de": FakeTranslation()})
        de = FakeLang("de", {"en": FakeTranslation()})
        return [fr, en, de]

    monkeypatch.setattr(Translator, "_installed_languages", fake_installed_languages)
    import argostranslate.package as real_package

    leg1 = FakePackage("fr", "en")
    leg2 = FakePackage("en", "de")
    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [leg1, leg2])
    installed = []
    monkeypatch.setattr(real_package, "install_from_path", lambda path: installed.append(path))

    tr = Translator()
    route = tr.ensure_route("fr", "de")

    assert route.kind == "pivot"
    assert leg1.downloaded is True
    assert leg2.downloaded is True
    assert len(installed) == 2


def test_ensure_route_clears_the_whole_cache_after_a_successful_install(monkeypatch):
    """Installing one package can create NEW pivot routes for UNRELATED
    pairs - the cache must be cleared wholesale, not just for the pair that
    triggered the install."""
    calls = {"n": 0}

    def fake_installed_languages(self):
        calls["n"] += 1
        if calls["n"] <= 2:
            # First call: resolve("fr","de") pre-install (unavailable).
            # Second call: resolve("es","pt") to seed the OTHER cache entry
            # (also unavailable at this point).
            return [FakeLang("fr", {}), FakeLang("de", {}), FakeLang("es", {}), FakeLang("pt", {})]
        # After install: fr->de now direct, AND es->pt now direct too (as if
        # the same install unlocked both - the scenario the wholesale clear
        # protects against silently missing).
        return [
            FakeLang("fr", {"de": FakeTranslation()}),
            FakeLang("de", {}),
            FakeLang("es", {"pt": FakeTranslation()}),
            FakeLang("pt", {}),
        ]

    monkeypatch.setattr(Translator, "_installed_languages", fake_installed_languages)
    import argostranslate.package as real_package

    pkg = FakePackage("fr", "de")
    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [pkg])
    monkeypatch.setattr(real_package, "install_from_path", lambda path: None)

    tr = Translator()
    assert tr.resolve("es", "pt").kind == "unavailable"  # seeds the cache, pre-install

    tr.ensure_route("fr", "de")  # triggers install + wholesale cache clear

    # Unrelated pair must be RE-resolved (not served from the stale cached
    # "unavailable"), proving the whole cache was cleared, not just fr->de.
    assert tr.resolve("es", "pt").kind == "direct"


def test_ensure_route_raises_package_unavailable_if_still_unavailable_after_install(monkeypatch):
    """Defensive: an install that somehow doesn't produce a usable route
    must still fail loudly, never pretend success."""
    monkeypatch.setattr(Translator, "_installed_languages", lambda self: [])
    import argostranslate.package as real_package

    pkg = FakePackage("es", "en")
    monkeypatch.setattr(real_package, "update_package_index", lambda: None)
    monkeypatch.setattr(real_package, "get_available_packages", lambda: [pkg])
    monkeypatch.setattr(real_package, "install_from_path", lambda path: None)

    tr = Translator()

    with pytest.raises(PackageUnavailable):
        tr.ensure_route("es", "en")


# --- Translator.translate(): hop execution + failure strings ----------------


def test_translate_empty_text_returns_empty_string():
    tr = Translator()
    assert tr.translate("") == ""
    assert tr.translate("   ") == ""


def test_translate_unavailable_route_returns_the_exact_v1_failure_string(monkeypatch):
    monkeypatch.setattr(Translator, "_installed_languages", lambda self: [])
    tr = Translator()

    assert tr.translate("hello", "fr", "de") == "[traducción no disponible]"


def test_translate_direct_route_executes_one_hop(monkeypatch):
    monkeypatch.setattr(
        Translator,
        "_installed_languages",
        lambda self: [FakeLang("en", {"es": FakeTranslation()}), FakeLang("es", {})],
    )
    import argostranslate.translate as real_translate

    calls = []

    def fake_translate(text, a, b):
        calls.append((text, a, b))
        return "Hola mundo"

    monkeypatch.setattr(real_translate, "translate", fake_translate)
    tr = Translator()

    result = tr.translate("Hello world", "en", "es")

    assert result == "Hola mundo"
    assert calls == [("Hello world", "en", "es")]


def test_translate_pivot_route_executes_two_hops_in_sequence(monkeypatch):
    monkeypatch.setattr(
        Translator,
        "_installed_languages",
        lambda self: [
            FakeLang("fr", {"en": FakeTranslation()}),
            FakeLang("en", {"de": FakeTranslation()}),
            FakeLang("de", {}),
        ],
    )
    import argostranslate.translate as real_translate

    calls = []

    def fake_translate(text, a, b):
        calls.append((text, a, b))
        return f"[{a}->{b}]{text}"

    monkeypatch.setattr(real_translate, "translate", fake_translate)
    tr = Translator()

    result = tr.translate("bonjour", "fr", "de")

    assert calls == [("bonjour", "fr", "en"), ("[fr->en]bonjour", "en", "de")]
    assert result == "[en->de][fr->en]bonjour"


def test_translate_default_args_are_en_to_es_matching_v1(monkeypatch):
    """AssistantStrategy calls self.translator.translate(answer) with NO
    from/to args - the defaults must reproduce v1.0.0's fixed EN->ES
    behavior exactly, so engines/assistant.py never has to change."""
    monkeypatch.setattr(
        Translator,
        "_installed_languages",
        lambda self: [FakeLang("en", {"es": FakeTranslation()}), FakeLang("es", {})],
    )
    import argostranslate.translate as real_translate

    calls = []
    monkeypatch.setattr(real_translate, "translate", lambda t, a, b: calls.append((t, a, b)) or "ok")
    tr = Translator()

    tr.translate("hi")

    assert calls == [("hi", "en", "es")]


def test_translate_exception_returns_the_exact_v1_error_string(monkeypatch):
    monkeypatch.setattr(
        Translator,
        "_installed_languages",
        lambda self: [FakeLang("en", {"es": FakeTranslation()}), FakeLang("es", {})],
    )
    import argostranslate.translate as real_translate

    def raising_translate(text, a, b):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(real_translate, "translate", raising_translate)
    tr = Translator()

    result = tr.translate("hi", "en", "es")

    assert result == "[error al traducir: model crashed]"
