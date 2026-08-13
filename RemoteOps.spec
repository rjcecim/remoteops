# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: gera RemoteOps.exe sem console

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('config', 'config'),
        ('remoteops/winget/templates', 'remoteops/winget/templates'),
    ],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'remoteops',
        'remoteops.bootstrap',
        'remoteops.ui.main_window',
        'remoteops.ui.tabs.psinfo',
        'remoteops.ui.tabs.appsearch',
        'remoteops.ui.tabs.winget',
        'remoteops.ui.winget.workers.winget_worker',
        'remoteops.winget.remote',
        'remoteops.winget.powershell_script',
        'remoteops.winget.constants',
        'remoteops.utils.psinfo',
        'remoteops.utils.remote_registry_query',
        'remoteops.utils.app_catalog',
        'remoteops.utils.redaction',
        'remoteops.utils.app_logging',
        'remoteops.utils.hosts',
        'remoteops.services.ops',
        'remoteops.core.models',
        'remoteops.core.win_cmd',
        'remoteops.core.conpty',
        'multiprocessing',
        'multiprocessing.spawn',
        'multiprocessing.resource_tracker',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tests', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RemoteOps',
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
    icon='assets/icon.ico',
)

import shutil
from pathlib import Path

_src_config = Path(SPECPATH) / 'config'
_dst_config = Path(DISTPATH) / 'config'
if _src_config.is_dir():
    if _dst_config.exists():
        shutil.rmtree(_dst_config)
    shutil.copytree(_src_config, _dst_config)
