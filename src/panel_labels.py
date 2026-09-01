"""Per-mode copy for chat_app.py's LivePanel, plus the Translator target-
language catalog for the composer's picker.

Extracted into a zero-import module so it is unit-testable without PySide6
or any of chat_app.py's heavy import chain (google.genai, faster_whisper,
argostranslate) - chat_app.py cannot be imported standalone in this project's
test environment (google-genai isn't installed), which is documented in
every prior slice's apply-progress. This module has none of those imports,
so the pure "which strings does each mode show" logic (design's change map:
"LivePanel's hardcoded assistant vocabulary becoming a per-mode label set")
stays testable even though chat_app.py itself never is.

Zero imports.
"""
from __future__ import annotations

ASSISTANT = "assistant"
TRANSLATOR = "translator"

_LABELS: dict[str, dict] = {
    ASSISTANT: {
        "header": "🎧 En vivo",
        "heard_placeholder": "Esperando audio...",
        # Assistant's Live panel cycles listening -> processing -> done
        # (unchanged from v1.0.0 - see chat_app.LivePanel.set_processing for
        # the "processing" state, which builds its own text with a live
        # second counter and so has no static label here).
        "action_listening": "■ Enviar y responder",
        "state_done": "✅ Respuesta lista",
        "action_done": "● Escuchar de nuevo",
        "hearing": {
            "idle": "🎧 Escuchando la llamada...",
            "speech": "🎤 La otra persona está hablando...",
            "capturing": "🔴 Grabando... pulsa «Enviar» cuando termine la pregunta.",
            "transcribing": "🧠 Entendiendo lo que dijo...",
        },
    },
    TRANSLATOR: {
        "header": "🌐 Traduciendo en vivo",
        "heard_placeholder": "Esperando audio para traducir...",
        # Translator is continuous (spec: "no send step") - the action
        # button only ever shows this single "stop" state while listening,
        # never a 3-way cycle.
        "action_listening": "■ Detener",
        "state_done": "✅ Traducción detenida",
        "action_done": "● Escuchar de nuevo",
        "hearing": {
            "idle": "🎧 Escuchando la llamada...",
            "speech": "🎤 La otra persona está hablando...",
            "capturing": "🌐 Traduciendo lo que se dijo...",
            "transcribing": "🌐 Traduciendo...",
        },
    },
}


def labels_for(mode: str) -> dict:
    """Label set for `mode`. Falls back to Assistant for any unrecognized
    value - same fallback convention as settings._valid_mode, so a stray or
    corrupt mode string never leaves the panel with no copy to show."""
    return _LABELS.get(mode, _LABELS[ASSISTANT])


# Target languages the composer's Translator picker offers. Restricted to
# what slice 6's investigation actually verified against the LIVE Argos
# package index (task 6.2): es<->en and es<->pt are DIRECT; fr/de/it have no
# direct pair to es and pivot through English - all three pivot legs
# (en<->fr, en<->de, en<->it) were confirmed available, so the pivot path
# always works, just with the quality warning (task 7.8). Not an exhaustive
# Argos language list - only the pairs this app's target audience needs.
TRANSLATOR_TARGET_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("es", "Español"),
    ("en", "Inglés"),
    ("fr", "Francés"),
    ("de", "Alemán"),
    ("it", "Italiano"),
    ("pt", "Portugués"),
)


def language_label(code: str) -> str:
    """Display name for a language code, or the code itself if unknown."""
    return dict(TRANSLATOR_TARGET_LANGUAGES).get(code, code)
