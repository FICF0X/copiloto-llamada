"""Make pip-installed CUDA libraries (cuBLAS, cuDNN) discoverable on Windows.

CTranslate2 needs cublas64_12.dll and the cuDNN DLLs at runtime, but Windows does
not search pip's nvidia/*/bin folders by default. Import this module BEFORE
faster_whisper so the directories are registered first.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def register_cuda_dlls() -> list[str]:
    """Register every nvidia/*/bin folder that contains DLLs. Returns added paths."""
    if sys.platform != "win32":
        return []
    try:
        import nvidia
    except ImportError:
        return []

    # `nvidia` is a namespace package -> no __file__, use __path__ instead.
    added: list[str] = []
    for base_str in getattr(nvidia, "__path__", []):
        for bin_dir in Path(base_str).glob("*/bin"):
            if any(bin_dir.glob("*.dll")):
                path = str(bin_dir)
                # add_dll_directory: for direct dependency resolution.
                os.add_dll_directory(path)
                # PATH: CTranslate2 loads cuBLAS/cuDNN lazily via LoadLibrary,
                # which searches PATH. add_dll_directory alone is not enough.
                if path not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
                added.append(path)
    return added


# Run on import.
_REGISTERED = register_cuda_dlls()
