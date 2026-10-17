# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['app.compressor', 'pikepdf', 'PIL', 'tkinterdnd2']
binaries += collect_dynamic_libs('pikepdf')
hiddenimports += collect_submodules('pikepdf')
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/Smallpdf/programs/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'doctest', 'pydoc', 'pydoc_data', 'lib2to3', 'test', 'tests', 'sqlite3', 'numpy', 'matplotlib', 'scipy', 'pandas', 'IPython', 'notebook', 'pytest', 'setuptools', 'pip', 'wheel', 'distutils', 'ssl', '_ssl', '_hashlib', 'PIL._avif', 'PIL._webp', 'PIL._imagingcms', 'PIL.ImageFont', 'PIL._imagingft', 'PIL.ImageTk', 'PIL._imagingtk', 'PIL.ImageQt', 'PIL.ImageGrab', 'PIL._imagingmorph', 'lxml.objectify', 'lxml.html', 'lxml.isoschematron', 'lxml.soupparser', 'lxml.doctestcompare', '_zstd', '_lzma', '_bz2', '_decimal', 'turtle', 'turtledemo', 'venv', 'ensurepip', 'msilib'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='PDFCompressor',
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
    icon=['D:/Smallpdf/assets/app.ico'],
)
