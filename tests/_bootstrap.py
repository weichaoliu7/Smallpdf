# -*- coding: utf-8 -*-
"""测试脚本统一路径引导。

测试脚本分布在 tests/ 的多个子目录中，各自计算相对路径既重复又易错。
本模块向上查找含 ``programs/`` 的目录作为项目根目录，并把 ``programs/``
与 ``tests/`` 加入模块搜索路径。

用法（在任意子目录的测试脚本中）：

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _bootstrap import ROOT, PROGRAMS, TESTS, REPORTS, BASELINES
"""
import os
import sys
import tempfile


def find_root(start: str = "") -> str:
    """向上查找含有 programs/ 与 README.md 的目录，作为项目根目录。"""
    cur = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "programs")) and \
                os.path.isfile(os.path.join(cur, "README.md")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise RuntimeError("未能定位项目根目录（未找到 programs/ 与 README.md）")


ROOT = find_root()
PROGRAMS = os.path.join(ROOT, "programs")
TESTS = os.path.join(ROOT, "tests")
REPORTS = os.path.join(TESTS, "reports")
BASELINES = os.path.join(TESTS, "baselines")
SOURCE_PDF = os.path.join(ROOT, "test_book.pdf")
EXE = os.path.join(ROOT, "PDFCompressor.exe")

# 测试中间产物（压缩出的 PDF）统一放在系统临时目录，避免污染项目目录。
# 跨脚本共享：verify_test.py 产出 out_<tier>.pdf，ordering_check.py 消费它们。
WORKDIR = os.path.join(tempfile.gettempdir(), "pdfc_tests")


def ensure_workdir() -> str:
    """确保共享工作目录存在并返回它。"""
    os.makedirs(WORKDIR, exist_ok=True)
    return WORKDIR


def ensure_reports() -> str:
    """确保测试报告目录存在并返回它。"""
    os.makedirs(REPORTS, exist_ok=True)
    return REPORTS


for _p in (PROGRAMS, TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
