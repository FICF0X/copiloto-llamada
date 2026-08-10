"""Override for pyinstaller-hooks-contrib's hook-webrtcvad.py.

The upstream hook does `copy_metadata('webrtcvad')`, assuming the PyPI
distribution is named "webrtcvad". This project depends on the
"webrtcvad-wheels" fork instead (same `import webrtcvad`, different
distribution name — see requirements.txt), so that call raises
PackageNotFoundError and aborts the whole build. This hook (found first via
CallAssist.spec's hookspath) copies metadata for the distribution that is
actually installed.
"""
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
