# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FaithView Pro Python sidecar.

Build with:
    pyinstaller faithview_sidecar.spec

Produces:
    dist/sidecar/sidecar  (or sidecar.exe on Windows)
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect all needed data files
bible_data = collect_data_files('bible_db', includes=['*.json'])

a = Analysis(
    ['sidecar.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # Bible data
        ('../bible_all_versions.json', '.'),
        ('../nkjv.json', '.'),
        ('../amplified.json', '.'),
        # ONNX model
        ('../onnx_model', 'onnx_model'),
        # FAISS index
        ('../index', 'index'),
        # Static files for the server
        ('../static', 'static'),
    ],
    hiddenimports=[
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'starlette',
        'pydantic',
        'websockets',
        'sounddevice',
        'scipy',
        'scipy.signal',
        'numpy',
        'faiss',
        'sentence_transformers',
        'transformers',
        'onnxruntime',
        'faster_whisper',
        'PIL',
        'bible_db',
        'config',
        'live_pipeline',
        'live_transcribe',
        'deepgram_transcribe',
        'quote_detect',
        'retrieve',
        'rerank',
        'score',
        'buffer',
        'intent_router',
        'context',
        'transcribe',
        'models',
        'search',
        'transcription_source',
        'paths',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'unittest',
    ],
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
    name='sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
