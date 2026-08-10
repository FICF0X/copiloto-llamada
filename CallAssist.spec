# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for CallAssist.

Build with build_exe.bat (repo root), which runs:
    <venv>\\Scripts\\python.exe -m PyInstaller CallAssist.spec --noconfirm

Produces an ONEDIR build (dist/CallAssist/CallAssist.exe + support files) —
NOT onefile: onefile re-extracts hundreds of MB to a temp dir on every launch,
which would make startup awful for an app this size (Whisper + Argos + Qt).

Ships WITHOUT CUDA (the `nvidia` packages are excluded below on purpose —
that's ~2 GB and GitHub Releases cap at 2 GB total). Whisper already falls
back to CPU when CUDA isn't available (see src/transcriber.py), so this is a
deliberate, working tradeoff, not an oversight.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

PROJECT_ROOT = Path(SPECPATH)  # noqa: F821 - injected by PyInstaller into the spec's globals

# --- PySide6: the app only imports QtCore/QtGui/QtWidgets (verified via grep
# over src/*.py) --- everything else below is Qt functionality this app never
# uses (3D, QML/Quick, charts, web engine, multimedia, dev tools, ...) and
# would otherwise get pulled in by PySide6's own PyInstaller hook.
EXCLUDED_PYSIDE6 = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer",
    "PySide6.QtAxContainer",
    "PySide6.QtExampleIcons",
]

datas = [
    (str(PROJECT_ROOT / "assets" / "icon.ico"), "assets"),
    # faster-whisper ships its VAD models (silero_*.onnx) as package data, not
    # code — PyInstaller only follows imports, so these need to be collected
    # explicitly or Whisper transcription fails at first real use.
    *collect_data_files("faster_whisper"),
]

binaries = [
    # ctranslate2's Python extension dynamically loads its own ctranslate2.dll
    # (and, if a GPU were present, cudnn64_9.dll) at runtime rather than via a
    # static PE import PyInstaller could follow — collect every binary in the
    # package explicitly so CPU translation still works.
    *collect_dynamic_libs("ctranslate2"),
]

hiddenimports = [
    # argostranslate.sbd unconditionally imports stanza/minisbd, and pulls
    # sacremoses/sentencepiece in transitively; none of this is reachable by
    # static analysis from the small set of names src/translator.py imports.
    *collect_submodules("argostranslate"),
    *collect_submodules("stanza"),
    "minisbd",
    "sacremoses",
    "sentencepiece",
    *collect_submodules("faster_whisper"),
    *collect_submodules("ctranslate2"),
]

a = Analysis(  # noqa: F821
    ["run_app.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # pyinstaller_hooks/ overrides pyinstaller-hooks-contrib's hook-webrtcvad.py,
    # which is broken for the webrtcvad-wheels fork this project actually uses
    # (see that file for why) — searched before the contrib/built-in hooks.
    hookspath=[str(PROJECT_ROOT / "pyinstaller_hooks")],
    excludes=["nvidia"] + EXCLUDED_PYSIDE6,
    noarchive=False,
)


def _is_unwanted(entry) -> bool:
    """Belt-and-suspenders: some hooks collect binaries by globbing a
    package's own folder rather than respecting `excludes`, so nvidia's CUDA
    DLLs (and PySide6.QtTranslations, which nothing above targets — it's data,
    not a module) can sneak back in. Strip them from the final table too."""
    dest = entry[0].lower().replace("\\", "/")
    return dest.startswith("nvidia/") or "/nvidia/" in dest or "pyside6/translations" in dest


a.binaries = [e for e in a.binaries if not _is_unwanted(e)]
a.datas = [e for e in a.datas if not _is_unwanted(e)]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CallAssist",
    console=False,  # windowed: no console window
    icon=str(PROJECT_ROOT / "assets" / "icon.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="CallAssist",
)
