# -*- coding: utf-8 -*-
"""
使用 PyInstaller 打包出独立的 Windows 单文件 exe。

目标机器无需安装 Python 或任何运行时，所有依赖都嵌入 exe 内。
不使用 UPX 压缩（可能被部分杀毒软件误报）。
"""
import os
import sys
import tempfile

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 项目根目录（assets/、exe 位于根目录）
DIST = ROOT                           # exe 直接输出到根目录，便于下载即运行

EXE_NAME = "PDFCompressor"

# 程序运行时用不到的模块，全部排除以减小 exe 体积
EXCLUDE = [
    # 开发 / 测试相关
    "unittest", "doctest", "pydoc", "pydoc_data", "lib2to3",
    "test", "tests", "sqlite3", "numpy", "matplotlib", "scipy", "pandas",
    "IPython", "notebook", "pytest", "setuptools", "pip", "wheel",
    "distutils",
    # 网络 / TLS：程序完全离线，均用不到
    "ssl", "_ssl", "_hashlib",
    # Pillow 中用不到的编解码插件（仅 AVIF 插件就有约 7.5 MB）
    "PIL._avif", "PIL._webp", "PIL._imagingcms",
    # Pillow 字体支持（约 2 MB，程序不渲染字体，仅开发期生成图标用）
    "PIL.ImageFont", "PIL._imagingft",
    # Pillow 中用不到的其他功能（界面不显示图片）
    "PIL.ImageTk", "PIL._imagingtk", "PIL.ImageQt",
    "PIL.ImageGrab", "PIL._imagingmorph",
    # lxml 用不到的子包（pikepdf 只需要 lxml.etree）
    "lxml.objectify", "lxml.html", "lxml.isoschematron",
    "lxml.soupparser", "lxml.doctestcompare",
    # 标准库中用不到的压缩 / 数值 / 杂项模块
    "_zstd", "_lzma", "_bz2", "_decimal",
    "turtle", "turtledemo", "venv", "ensurepip", "msilib",
]

args = [
    os.path.join(HERE, "main.py"),
    "--name=" + EXE_NAME,
    "--onefile",          # 打包为单个 exe
    "--windowed",         # 图形界面程序，不显示控制台黑窗
    "--clean",
    "--noconfirm",
    "--optimize=2",       # 去除断言与文档字符串，进一步缩小体积
    "--distpath=" + DIST,          # exe 输出到根目录
    # spec 与构建中间产物放进 programs/，避免污染项目根目录
    "--specpath=" + HERE,
    "--workpath=" + os.path.join(tempfile.gettempdir(), "pdfc_build"),
    "--icon=" + os.path.join(ROOT, "assets", "app.ico"),
    "--hidden-import=app.compressor",
    "--hidden-import=pikepdf",
    "--hidden-import=PIL",
    "--hidden-import=tkinterdnd2",
    "--collect-all=tkinterdnd2",
    "--collect-binaries=pikepdf",
    "--collect-submodules=pikepdf",
    "--log-level=WARN",
]
for module in EXCLUDE:
    args.append("--exclude-module=" + module)

if __name__ == "__main__":
    print("正在构建 %s，需要几分钟时间..." % EXE_NAME)
    PyInstaller.__main__.run(args)
    exe = os.path.join(DIST, EXE_NAME + ".exe")
    if os.path.exists(exe):
        size = os.path.getsize(exe)
        print("\n构建完成：%s" % exe)
        print("体积：%.2f MB" % (size / 1024 / 1024))
    else:
        print("\n未找到输出文件，请检查构建日志。")
        sys.exit(1)
