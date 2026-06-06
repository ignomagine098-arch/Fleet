# -*- mode: python ; coding: utf-8 -*-
"""
launcher.spec  â€”  Single-file EXE, zero source folders shipped
================================================================
Build from the final_fm/ directory:
    cd C:\\Users\\nagra\\Desktop\\Fleet\\final_fm
    pyinstaller launcher.spec --clean

After building, the dist/ folder contains only:
    WarehouseSystem.exe      <- ship this to the client, no folders needed
"""

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# â”€â”€ Project paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SPEC_DIR    = os.path.dirname(os.path.abspath(SPEC))   # final_fm/
FM_ROOT     = os.path.join(SPEC_DIR, 'FM_latest')
CLIENT_ROOT = os.path.join(SPEC_DIR, 'server-client_code')

# â”€â”€ Hidden imports (PyInstaller can't auto-detect these) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CRITICAL: We MUST add these to sys.path so collect_submodules() can find them!
import sys
for p in [FM_ROOT, CLIENT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

hidden = (
    collect_submodules('uvicorn')
    + collect_submodules('starlette')
    + collect_submodules('anyio')
    + collect_submodules('fastapi')
    + collect_submodules('PyQt5')
    # FM_latest subpackages â€” many modules are lazily imported and missed
    # by static analysis (e.g. ui.common.login_screen, ui.main_window, etc.)
    + collect_submodules('ui')
    + collect_submodules('ui.maps')
    + collect_submodules('services')
    + collect_submodules('data_manager')
    + collect_submodules('utils')
    + collect_submodules('robot_navigation')
    + collect_submodules('config')
    + collect_submodules('api')
    + [
        'ui.maps.zone_config',
        'ui.maps.robot_sprite',
        'ui.maps.map_management',
        'ui.maps.map_viewer',
        'path_utils',
        'PyQt5.QtSvg', 'PyQt5.sip',
        'bcrypt', 'bcrypt._bcrypt',
        'cryptography', 'cryptography.fernet',
        'sqlalchemy', 'sqlalchemy.ext.declarative', 'sqlalchemy.orm',
        'mysql.connector',
        'dotenv', 'python_dotenv',
    ]
)

# â”€â”€ Static data files (images, icons, styles only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Format: (source_path, dest_folder_inside_bundle)
# We do NOT add FM_latest/*.py or server-client_code/*.py here â€”
# those are compiled into the EXE via pathex.
added_datas = [
    (os.path.join(FM_ROOT, 'resources', 'ignoLogo.ico'),       'resources'),
    (os.path.join(FM_ROOT, 'resources', 'logo.png'),           'resources'),
    (os.path.join(FM_ROOT, 'resources', 'logo1.png'),          'resources'),
    (os.path.join(FM_ROOT, 'resources', 'jk.png'),             'resources'),
    (os.path.join(FM_ROOT, 'resources', 'styles'),             'resources/styles'),
    # .env must sit next to the EXE so FERNET_KEY and credentials load correctly
    (os.path.join(FM_ROOT, '.env'),                            '.'),
    # software_config.csv â€” bundled as read-only template. launcher.py copies it
    # to dist/ on first run so the user can edit dist/software_config.csv freely.
    (os.path.join(FM_ROOT, 'software_config.csv'),             '_default_config'),
]

# Collect any data files uvicorn/starlette ship (templates etc.)
added_datas += collect_data_files('uvicorn')
added_datas += collect_data_files('starlette')

# â”€â”€ EXCLUDES: heavy packages installed globally but NOT used by this app â”€â”€â”€â”€â”€
# This cuts build time from 30+ minutes to ~3-5 minutes.
excluded = [
    # AI / ML / Scientific (not needed)
    'torch', 'torchvision', 'torchaudio',
    'tensorflow', 'keras',
    'matplotlib',
    'sklearn', 'skimage', 'numba', 'cupy', 'jax',
    'cv2',
    'scipy', 'scipy.special._cdflib',
    'transformers', 'diffusers', 'huggingface_hub',
    'fsspec', 's3fs', 'gcsfs',
    'dask', 'ray', 'pyarrow', 'polars',
    'bokeh', 'plotly', 'seaborn',
    # Jupyter / notebooks
    'jupyter', 'notebook', 'ipython', 'IPython',
    # GUI toolkits other than PyQt5
    'tkinter', '_tkinter', 'wx', 'gtk',
    # Dev / build tools  (do NOT add distutils/setuptools â€” PyInstaller hooks them internally)
    'git', 'gitdb',
    # Optional/probe-only imports from hooks (safe to exclude if unused)
    'sip', 'pysqlite2', 'MySQLdb', 'psycopg2',
    'test', 'tests', 'unittest',
]

# â”€â”€ Analysis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
a = Analysis(
    ['launcher.py'],
    # pathex: PyInstaller searches these folders for imports and COMPILES
    # all .py files into the EXE archive â€” no source is shipped to clients.
    pathex=[SPEC_DIR, FM_ROOT, CLIENT_ROOT],
    binaries=[],
    datas=added_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# â”€â”€ Single-file EXE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,       # must be here for --onefile
    a.zipfiles,
    a.datas,
    [],
    name='WarehouseSystem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # Change to False after confirming it works
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(FM_ROOT, 'resources', 'ignoLogo.ico'),
)

