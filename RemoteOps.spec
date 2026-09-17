# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: gera RemoteOps-<versão>-Build<n>.exe sem console (sem RemoteOps.exe).

from pathlib import Path

from remoteops.core.version import __build__, __version__

EXE_BASENAME = f"RemoteOps-{__version__}-Build{__build__}"

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
        'remoteops.ui.tabs.hostsearch',
        'remoteops.ui.tabs.winget',
        'remoteops.ui.tabs.message',
        'remoteops.ui.tabs.printers',
        'remoteops.ui.tabs.batchinstall',
        'remoteops.ui.winget.workers.winget_worker',
        'remoteops.services.messaging',
        'remoteops.services.msg_style',
        'remoteops.services.printers',
        'remoteops.services.batch_install',
        'remoteops.winget.remote',
        'remoteops.winget.powershell_script',
        'remoteops.winget.constants',
        'remoteops.utils.psping',
        'remoteops.utils.host_reachability',
        'remoteops.ui.tabs.connectivity',
        'remoteops.ui.tabs.energia',
        'remoteops.ui.tabs.contas_locais',
        'remoteops.utils.psshutdown',
        'remoteops.services.power',
        'remoteops.services.local_accounts',
        'remoteops.utils.local_accounts',
        'remoteops.utils.inventory.remote_exec',
        'remoteops.utils.psinfo',
        'remoteops.utils.printers',
        'remoteops.utils.printer_settings',
        'remoteops.utils.sessions',
        'remoteops.utils.hostsearch',
        'remoteops.utils.network_scan',
        'remoteops.utils.remote_registry_query',
        'remoteops.utils.app_catalog',
        'remoteops.utils.redaction',
        'remoteops.utils.app_logging',
        'remoteops.utils.hosts',
        'remoteops.services.ops',
        'remoteops.core.models',
        'remoteops.core.win_cmd',
        'remoteops.core.win_cmdline',
        'remoteops.core.console_codec',
        'remoteops.core.process_runner',
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
    name=EXE_BASENAME,
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

_src_config = Path(SPECPATH) / 'config'
_dst_config = Path(DISTPATH) / 'config'
if _src_config.is_dir():
    if _dst_config.exists():
        shutil.rmtree(_dst_config)
    shutil.copytree(_src_config, _dst_config)

# Não deixar o nome legado RemoteOps.exe no dist (builds antigos ou cópia de release).
_stale = Path(DISTPATH) / 'RemoteOps.exe'
if _stale.is_file():
    _stale.unlink()
