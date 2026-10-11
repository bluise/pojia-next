# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/WorkBuddy/2026-10-11-15-10-46/pojia-next/破甲一键通.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/WorkBuddy/2026-10-11-15-10-46/pojia-next/_build/_pojia_src.py', '.'), ('D:/WorkBuddy/2026-10-11-15-10-46/pojia-next/破甲一键通.py', '.')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='破甲一键通',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
