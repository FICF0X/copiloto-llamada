"""Offline English -> Spanish translation via Argos Translate.

No API and no Gemini tokens: Argos runs a local neural model. The en->es package
is downloaded once on first use (needs internet that one time), then every
translation is fully offline and never rate-limited mid-call.
"""
from __future__ import annotations

FROM_CODE = "en"
TO_CODE = "es"


class Translator:
    """Translates English text to Spanish using a local Argos model."""

    def __init__(self) -> None:
        self.ready = False
        try:
            self._ensure_model()
            self.ready = True
        except Exception as exc:  # noqa: BLE001 - translation is best-effort
            print(f"[translator] Could not set up offline translation: {exc}")

    def _ensure_model(self) -> None:
        """Install the en->es package if it is not already present."""
        import argostranslate.package
        import argostranslate.translate

        installed = argostranslate.translate.get_installed_languages()
        if self._has_pair(installed):
            return

        # One-time download + install (requires internet just this once).
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
        pkg = next(
            (p for p in available if p.from_code == FROM_CODE and p.to_code == TO_CODE),
            None,
        )
        if pkg is None:
            raise RuntimeError("Argos en->es package not found in the index.")
        argostranslate.package.install_from_path(pkg.download())

    @staticmethod
    def _has_pair(languages) -> bool:
        from_lang = next((l for l in languages if l.code == FROM_CODE), None)
        to_lang = next((l for l in languages if l.code == TO_CODE), None)
        if not (from_lang and to_lang):
            return False
        try:
            return from_lang.get_translation(to_lang) is not None
        except Exception:  # noqa: BLE001
            return False

    def translate(self, text: str) -> str:
        """Translate English text to Spanish. Returns '' for empty input."""
        if not text.strip():
            return ""
        if not self.ready:
            return "[traducción no disponible]"
        import argostranslate.translate

        try:
            return argostranslate.translate.translate(text, FROM_CODE, TO_CODE)
        except Exception as exc:  # noqa: BLE001
            return f"[error al traducir: {exc}]"


def _test() -> None:
    print("Setting up offline translator (downloads the en->es model once)...")
    tr = Translator()
    print(f"[OK] Translator ready: {tr.ready}\n")
    sample = "Hello, can you tell me about your experience with Python?"
    print(f"EN: {sample}")
    print(f"ES: {tr.translate(sample)}")


if __name__ == "__main__":
    _test()
