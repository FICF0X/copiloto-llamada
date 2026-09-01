"""Manual smoke tool for src/translator.py - NOT part of the pytest suite.

Run this by hand (needs internet + real argostranslate, downloads real
packages) to sanity-check route resolution and translation against the LIVE
Argos index and whatever is actually installed on this machine:

    py -3.12 scripts/smoke_translator.py

What it does, in order:
1. Prints resolve_route() for the pairs the design cares about
   (es<->en/pt/fr/de/it), against whatever is ALREADY installed - no
   downloads at this step.
2. Optionally installs the pair you pick and re-resolves it, so you can see
   a route flip from "unavailable"/"pivot" to its post-install state.
3. Runs one real translation through it.

This exists because slice 6's automated tests use fakes for
resolve_route()'s installed_languages parameter (deliberately - they must
run with no network and no multi-hundred-MB downloads). This script is the
tool for verifying the REAL library still behaves the way those fakes
assume, on demand, whenever the owner wants to double-check it (e.g. after
an argostranslate version bump).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.translator import PIVOT, Translator, resolve_route  # noqa: E402

PAIRS = [("es", "en"), ("es", "pt"), ("es", "fr"), ("es", "de"), ("es", "it")]


def main() -> None:
    tr = Translator()
    installed = tr._installed_languages()
    installed_codes = sorted(lang.code for lang in installed)
    print(f"Installed language codes: {installed_codes or '(none)'}\n")

    print("Route resolution against what's currently installed (no network):")
    for from_code, to_code in PAIRS:
        route = resolve_route(installed, from_code, to_code)
        print(f"  {from_code}->{to_code}: {route.kind:<11} hops={route.hops}")
        route_back = resolve_route(installed, to_code, from_code)
        print(f"  {to_code}->{from_code}: {route_back.kind:<11} hops={route_back.hops}")

    print()
    answer = input(
        "Install+translate one pair now? This downloads a REAL Argos package "
        f"(y/N, or type 'from,to' e.g. 'es,{PIVOT}'): "
    ).strip()
    if not answer or answer.lower() == "n":
        print("Skipped. Nothing downloaded.")
        return

    if "," in answer:
        from_code, to_code = (part.strip() for part in answer.split(",", 1))
    else:
        from_code, to_code = "es", "en"

    def status(msg: str) -> None:
        print(f"  [status] {msg}")

    print(f"\nEnsuring {from_code}->{to_code}...")
    route = tr.ensure_route(from_code, to_code, on_status=status)
    print(f"Route ready: {route.kind} hops={route.hops}")

    sample = "Hello, this is a smoke test of the offline translator."
    result = tr.translate(sample, from_code, to_code)
    print(f"\n{from_code.upper()}: {sample}")
    print(f"{to_code.upper()}: {result}")


if __name__ == "__main__":
    main()
