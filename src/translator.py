"""Offline translation via Argos Translate, resolved as explicit hop routes.

No API and no Gemini tokens: Argos runs local neural models. A package is
downloaded once per language pair (needs internet that one time), then every
translation using it is fully offline and never rate-limited mid-call.

Multi-pair rewrite (slice 6): `Translator.__init__` no longer downloads
anything - construction is cheap and side-effect free. Installation is a
separate, explicit, blocking call (`ensure_route`) meant to be run off the
UI thread (see `chat_app.PairInstaller`), triggered when the user picks a
target language or the moment the source language locks - never when they
press "Escuchar" (that would freeze the start of a call on a download).

Route resolution never trusts argostranslate's own pivot-chaining to decide
what counts as a genuinely DIRECT pair - see resolve_route()'s docstring for
why, verified empirically against argostranslate 1.11.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# English is the only pivot this app ever routes through. Every language
# Argos ships has an en<->X package, which is what makes "pivot through
# English" a reliable fallback for a pair with no direct package.
PIVOT = "en"


class PackageIndexError(RuntimeError):
    """update_package_index() (or fetching the live package list) failed -
    almost always a network problem."""


class PackageUnavailable(RuntimeError):
    """Neither a direct package nor both legs of an English-pivot package
    exist for a pair in the live Argos index."""


class PackageInstallError(RuntimeError):
    """A package was found in the live index but installing it failed."""


@dataclass(frozen=True)
class Route:
    """How to get from one language to another using installed Argos
    packages. `hops` is executed sequentially and explicitly by
    Translator.translate() - one argostranslate.translate.translate() call
    per consecutive pair in hops - never left to library-internal chaining.
    """

    kind: str  # "direct" | "pivot" | "unavailable"
    hops: tuple[str, ...] = ()  # e.g. ("es", "en") or ("fr", "en", "de")


def _is_composite(translation: object) -> bool:
    """True when `translation` is an argostranslate CompositeTranslation -
    i.e. NOT a genuinely installed single-hop package, but a translation the
    library synthesized on the fly by chaining two other translations.

    Why this matters (verified against argostranslate 1.11.0, not assumed):
    `argostranslate.translate.get_installed_languages()` pre-builds a
    CompositeTranslation for EVERY pair reachable via any intermediate
    language and appends it into `Language.translations_from` - so
    `from_lang.get_translation(to_lang) is not None` is true for pivot-only
    pairs too, not just genuinely direct ones. Trusting that check alone
    would silently mislabel a pivot route as "direct" and skip the UI's
    pivot-quality warning (the spec requires that warning is never silent).

    Detection is duck-typed on the `t1`/`t2` attributes CompositeTranslation
    carries (its chained legs) - real PackageTranslation/CachedTranslation/
    IdentityTranslation objects never have them. No argostranslate import
    needed here, which keeps this module's route-resolution path pure and
    testable with 4-line fakes, per the design's test seam.
    """
    return hasattr(translation, "t1") and hasattr(translation, "t2")


def resolve_route(installed_languages, from_code: str, to_code: str) -> Route:
    """Pure: does from_code have a genuinely DIRECT installed package to
    to_code, or must the translation pivot through English? Never chains
    implicitly - Translator.translate() executes `hops` itself, one call per
    hop, whether or not argostranslate would also chain internally (that
    behavior is unverified/irrelevant either way - see PackageIndexError's
    module docstring and the design's PINNED DECISION 3).

    `installed_languages` mirrors the pre-existing `_has_pair` seam: a list
    of objects exposing `.code` and `.get_translation(other) -> object|None`.
    """
    from_lang = _find(installed_languages, from_code)
    to_lang = _find(installed_languages, to_code)

    if from_lang is not None and to_lang is not None:
        translation = _safe_get_translation(from_lang, to_lang)
        if translation is not None and not _is_composite(translation):
            return Route(kind="direct", hops=(from_code, to_code))

    if from_code != PIVOT and to_code != PIVOT:
        pivot_lang = _find(installed_languages, PIVOT)
        if from_lang is not None and pivot_lang is not None and to_lang is not None:
            leg1 = _safe_get_translation(from_lang, pivot_lang)
            leg2 = _safe_get_translation(pivot_lang, to_lang)
            if leg1 is not None and leg2 is not None:
                return Route(kind="pivot", hops=(from_code, PIVOT, to_code))

    return Route(kind="unavailable", hops=())


def _find(languages, code: str):
    return next((lang for lang in languages if lang.code == code), None)


def _safe_get_translation(from_lang, to_lang):
    try:
        return from_lang.get_translation(to_lang)
    except Exception:  # noqa: BLE001 - a bad fake/library edge case, never fatal
        return None


class Translator:
    """Resolves and executes Argos routes across arbitrary language pairs."""

    def __init__(self) -> None:
        # Per-instance route cache, keyed by (from_code, to_code). Cleared
        # WHOLESALE after any successful install: installing one package can
        # create new pivot routes for unrelated pairs, so per-key
        # invalidation would lie. No disk cache - the ground truth (what's
        # actually installed) can change outside the app between runs.
        self._routes: dict[tuple[str, str], Route] = {}

    def _installed_languages(self):
        import argostranslate.translate

        return argostranslate.translate.get_installed_languages()

    def resolve(self, from_code: str, to_code: str) -> Route:
        key = (from_code, to_code)
        if key not in self._routes:
            self._routes[key] = resolve_route(self._installed_languages(), from_code, to_code)
        return self._routes[key]

    def ensure_route(
        self,
        from_code: str,
        to_code: str,
        on_status: Callable[[str], None] | None = None,
    ) -> Route:
        """Blocking. Installs whatever Argos packages are missing to make
        from_code->to_code usable, then returns the resulting Route.

        Meant to run off the UI thread (see chat_app.PairInstaller) or on the
        worker thread at the moment a source language locks - never as part
        of pressing "Escuchar", which must never freeze on a download.

        Raises PackageIndexError / PackageUnavailable / PackageInstallError
        instead of printing (the v1.0.0 code printed into a pythonw process
        with no console - a silent failure nobody could ever see).
        """

        def status(message: str) -> None:
            if on_status is not None:
                on_status(message)

        route = self.resolve(from_code, to_code)
        if route.kind != "unavailable":
            return route

        status("Actualizando el índice de paquetes de traducción...")
        import argostranslate.package

        try:
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
        except Exception as exc:  # noqa: BLE001
            raise PackageIndexError(str(exc)) from exc

        packages = self._packages_for_route(available, from_code, to_code)
        if not packages:
            raise PackageUnavailable(
                f"No hay paquete directo ni pivote via {PIVOT} disponible para "
                f"{from_code}->{to_code}."
            )

        for pkg in packages:
            status(f"Descargando paquete de traducción {pkg.from_code}->{pkg.to_code}...")
            try:
                argostranslate.package.install_from_path(pkg.download())
            except Exception as exc:  # noqa: BLE001
                raise PackageInstallError(str(exc)) from exc

        # A successful install can open up routes for OTHER pairs too (e.g.
        # installing en->de also enables an es->de pivot) - clearing the
        # whole cache, not just this key, is the only way that stays honest.
        self._routes.clear()

        route = self.resolve(from_code, to_code)
        if route.kind == "unavailable":
            raise PackageUnavailable(
                f"Los paquetes se instalaron pero {from_code}->{to_code} sigue sin "
                "ruta disponible."
            )
        return route

    @staticmethod
    def _packages_for_route(available, from_code: str, to_code: str):
        """Which live-index packages must be installed for from_code->to_code
        to become usable: the direct one if the index has it, else both legs
        of the English pivot (only if the index has BOTH - a half-available
        pivot is not installed at all, so translate() still fails clean)."""
        direct = next(
            (p for p in available if p.from_code == from_code and p.to_code == to_code),
            None,
        )
        if direct is not None:
            return [direct]

        if from_code == PIVOT or to_code == PIVOT:
            return []  # already tried "direct" above; no pivot through itself

        leg1 = next(
            (p for p in available if p.from_code == from_code and p.to_code == PIVOT),
            None,
        )
        leg2 = next(
            (p for p in available if p.from_code == PIVOT and p.to_code == to_code),
            None,
        )
        if leg1 is not None and leg2 is not None:
            return [leg1, leg2]
        return []

    def translate(self, text: str, from_code: str = "en", to_code: str = "es") -> str:
        """Translate text along from_code->to_code, executing each hop of
        the resolved Route as its own explicit call. Returns '' for empty
        input. Default args (en->es) preserve v1.0.0/AssistantStrategy's
        existing single-argument call site byte-for-byte - only
        TranslatorStrategy ever passes explicit codes.
        """
        if from_code == to_code:
            # Argos happily builds an identity route for this; running the
            # text through it just costs time and can mangle punctuation.
            return text
        if not text.strip():
            return ""

        route = self.resolve(from_code, to_code)
        if route.kind == "unavailable":
            return "[traducción no disponible]"

        import argostranslate.translate

        try:
            current = text
            for a, b in zip(route.hops, route.hops[1:]):
                current = argostranslate.translate.translate(current, a, b)
            return current
        except Exception as exc:  # noqa: BLE001
            return f"[error al traducir: {exc}]"


def _test() -> None:
    print("Resolving es->en (installs the package if missing)...")
    tr = Translator()

    def status(msg: str) -> None:
        print(f"  [status] {msg}")

    route = tr.ensure_route("en", "es", on_status=status)
    print(f"[OK] route: {route}")
    sample = "Hello, can you tell me about your experience with Python?"
    print(f"EN: {sample}")
    print(f"ES: {tr.translate(sample, 'en', 'es')}")


if __name__ == "__main__":
    _test()
