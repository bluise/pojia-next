# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['破甲GUI.py'],
    pathex=[],
    binaries=[],
    # v8.8 本地核心版：核心脚本 破甲一键通.py 随 exe 一起打包（data），
    # 启动时直接从 _MEIPASS 加载 —— 不再联网握手，不再要求输入 QQ 群号。
    # 核心自带的封条自检照常生效，被改过的核心依旧当场拒启（exit 3）。
    # cryptography 仍排除（卡巴斯基 A/B 实测）；
    # ⚠ cffi/pycparser 必须保留 —— clr_loader.ffi 顶层 import cffi，
    #   排掉它 webview 的 .NET 加载链直接断，exe 会退回浏览器模式。
    datas=[('icon.ico', '.'), ('donate-qr.png', '.'),
           ('破甲一键通.py', '.')],
    hiddenimports=['webview', 'webview.platforms.winforms', 'webview.platforms.edgechromium',
                   'clr_loader', 'clr_loader.ffi', 'pythonnet',
                   'argparse', 'base64', 'ctypes', 'datetime', 'glob', 'hashlib',
                   'hmac', 'importlib', 'json', 'platform', 're', 'secrets', 'shlex',
                   'shutil', 'ssl', 'string', 'subprocess', 'tomllib', 'unicodedata',
                   'urllib.parse', 'urllib.request', 'webbrowser', 'winreg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # v8.5：砍掉用不到的大块标准库 —— 缩小体积、加快单文件自解压启动
    excludes=['tkinter', 'test', 'tests', 'pydoc_data', 'unittest', 'xmlrpc',
              'pdb', 'doctest', 'pydoc', 'lib2to3', 'cryptography'],
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
    name='破甲一键通GUI',
    icon='icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
