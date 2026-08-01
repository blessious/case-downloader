# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_all

playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
codesign_identity = os.environ.get("APPLE_SIGNING_IDENTITY") or None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=playwright_binaries,
    datas=playwright_datas + [("app/static", "app/static")],
    hiddenimports=playwright_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Case PDF Downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=codesign_identity,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Case PDF Downloader",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Case PDF Downloader.app",
        bundle_identifier="com.casepdfdownloader.app",
        info_plist={
            "CFBundleDisplayName": "Case PDF Downloader",
            "CFBundleName": "Case PDF Downloader",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
