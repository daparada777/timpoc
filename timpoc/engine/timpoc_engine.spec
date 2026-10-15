# -*- mode: python ; coding: utf-8 -*-
"""
timpoc — сборка движка распознавания в один исполняемый файл.

Запуск (на Windows, для получения engine.exe):
    python -m pip install -r requirements.txt pyinstaller
    python -m PyInstaller timpoc_engine.spec --noconfirm

Почему onedir, а не onefile:
  onefile при каждом запуске распаковывает всё во временную папку. На Windows
  это медленно, и антивирус проверяет каждую распакованную DLL — задержка
  достигает десятков секунд. onedir стартует сразу.

Что намеренно НЕ пакуем:
  * модели Whisper (~0.5–3 ГБ) — они скачиваются один раз по кнопке в панели.
    Паковать их в установщик означало бы огромный дистрибутив, который
    большинству не нужен целиком.
  * тесты и pytest.
"""

block_cipher = None

a = Analysis(
    ["timpoc/cli.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        # faster-whisper и ctranslate2 подгружают часть модулей динамически —
        # без явного указания PyInstaller их не находит, и exe падает при старте.
        "faster_whisper",
        "ctranslate2",
        "huggingface_hub",
        "tokenizers",
        "onnxruntime",
        "av",
        "numpy",
        "timpoc.server",
        "timpoc.pipeline",
        "timpoc.recognize",
        "timpoc.segmentation",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "torch",  # намеренно: faster-whisper работает без PyTorch, а он весит гигабайты
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
    [],
    exclude_binaries=True,
    name="timpoc-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX часто вызывает ложные срабатывания антивируса
    console=True,            # консоль нужна: движок пишет журнал и его видно при отладке
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="timpoc-engine",
)
