# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the windowed build.

The same binaries as the console build, with two differences that matter.
console=False, because a windowed Windows executable that keeps a console
flashes a black box on every launch. And tkinter is not excluded -- it is
the entire user interface here, whereas the console build excludes it to
stay small.
"""

import os

binaries = []
for entry in sorted(os.listdir("bin")):
    path = os.path.join("bin", entry)
    if os.path.isfile(path):
        binaries.append((path, "bin"))

analysis = Analysis(
    ["pub2idml_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["unittest", "pydoc", "test"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="pub2idml-gui",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
