"""Engine strategies: what CopilotWorker does with a transcribed utterance.

Only re-exports the base contract here. Concrete strategies (assistant,
translation) are imported explicitly by their own module path so importing
`src.engines` never pulls in a concrete strategy's dependencies.
"""
from __future__ import annotations

from src.engines.base import EngineCallbacks, EngineResult, EngineStrategy, Utterance

__all__ = ["EngineCallbacks", "EngineResult", "EngineStrategy", "Utterance"]
