# -*- coding: utf-8 -*-
"""并行规模与降级路径的耗时基准（用于选定默认工作进程数）。

用法：python tests/bench_workers.py
对比：串行（含微优化）、线程池、进程池 4/8/12/20 档。
"""
from __future__ import annotations

import os
import sys
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS  # noqa: E402

from app import compressor as C  # noqa: E402

def run(label: str, workers: int | None, use_mp: bool = True) -> None:
    if workers is None:
        os.environ.pop("PDFC_WORKERS", None)
    else:
        os.environ["PDFC_WORKERS"] = str(workers)
    os.environ["PDFC_DISABLE_MP"] = "0" if use_mp else "1"
    C._MP_DISABLED = False
    src = os.path.join(ROOT, "test_book.pdf")
    t = time.perf_counter()
    plans = C.precompute_pdf(src)
    dt = time.perf_counter() - t
    n_fr = sum(1 for p in plans if p["status"] == "frontier")
    print("%-22s %8.2f s   前沿图 %d 张   加速 %5.2fx"
          % (label, dt, n_fr, 230.24 / dt))


def main() -> None:
    print("基线（优化前，串行）: 230.24 s\n")
    run("串行（含微优化）", 1, use_mp=False)
    run("线程池 12", 12, use_mp=False)
    for w in (4, 8, 12, 16, 20, 31):
        run("进程池 %d" % w, w)


if __name__ == "__main__":
    main()
