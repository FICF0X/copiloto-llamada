"""Brain must hand the prompt composer its arguments in the right order.

All four are plain strings, so swapping the preset context with the call
context — or the answer language with the length — type-checks, runs, and
silently corrupts every prompt while the rest of the suite stays green.
Only this test stands between that mistake and a live call.

google.genai is stubbed rather than installed: nothing here touches the
network, and the import only exists so `src.brain` can be imported at all.
"""
from __future__ import annotations

import sys
import types

import pytest


def _stub_google_genai() -> None:
    if "google.genai" in sys.modules:
        return
    try:
        # Prefer the REAL `google` namespace package (installed transitively
        # by e.g. `protobuf`) if one is importable. `sys.modules.setdefault`
        # used to blindly insert a bare `types.ModuleType("google")` when
        # nothing had imported `google` yet - but that fake module has no
        # `__path__`, so it permanently shadows the real namespace package
        # for the REST of the test session (sys.modules is process-global
        # and never rolled back). Any later test that needs a genuine
        # `google.*` submodule (e.g. `google.protobuf`, pulled in
        # transitively by `argostranslate` -> `stanza`) would then fail with
        # "'google' is not a package" purely because of import ORDER -
        # discovered while adding slice 6's translator tests.
        import google
    except ImportError:
        google = sys.modules.setdefault("google", types.ModuleType("google"))
    genai = types.ModuleType("google.genai")
    genai_types = types.ModuleType("google.genai.types")

    class _Any:
        def __init__(self, *args, **kwargs) -> None:
            self.__dict__.update(kwargs)

    for name in ("GenerateContentConfig", "ThinkingConfig", "HttpOptions", "Content", "Part"):
        setattr(genai_types, name, _Any)
    genai.types = genai_types
    genai.Client = _Any
    google.genai = genai
    sys.modules["google.genai"] = genai
    sys.modules["google.genai.types"] = genai_types


_stub_google_genai()

from src import brain as brain_module  # noqa: E402
from src.brain import Brain  # noqa: E402


@pytest.fixture()
def unconfigured_brain(monkeypatch):
    """A Brain without touching validate() or building a real client."""
    monkeypatch.setattr(brain_module, "validate", lambda: None)
    monkeypatch.setattr(brain_module.genai, "Client", lambda **kwargs: object())
    return Brain()


def test_make_config_passes_prompt_arguments_in_the_declared_order(
    unconfigured_brain, monkeypatch
):
    captured: dict[str, object] = {}

    def spy(preset_context, call_context, answer_language, length):
        captured.update(
            preset_context=preset_context,
            call_context=call_context,
            answer_language=answer_language,
            length=length,
        )
        return "composed"

    monkeypatch.setattr(brain_module, "compose_system_instruction", spy)

    unconfigured_brain.set_preset("PRESET ROLE", "es")
    unconfigured_brain.set_context("CALL BRIEFING")
    unconfigured_brain.set_length("detailed")
    unconfigured_brain._make_config()

    assert captured["preset_context"] == "PRESET ROLE"
    assert captured["call_context"] == "CALL BRIEFING"
    assert captured["answer_language"] == "es"
    assert captured["length"] == "detailed"
