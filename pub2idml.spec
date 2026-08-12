# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: bundles pubdump plus its native DLLs into one .exe.

Everything in bin/ is shipped under a bin/ folder inside the bundle, which
is where pubidml.convert._locate_pubdump looks when sys._MEIPASS is set.
Run `make dlls` first on Windows so bin/ holds the MinGW runtime libraries
alongside pubdump.exe.
"""

import os

binaries = []
for entry in sorted(os.listdir("bin")):
    path = os.path.join("bin", entry)
    if os.path.isfile(path):
        binaries.append((path, "bin"))

analysis = Analysis(
    ["pub2idml.py"],
    pathex=["."],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # The converter is standard library only; excluding the heavyweight
    # optional modules keeps the executable small.
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="pub2idml",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
