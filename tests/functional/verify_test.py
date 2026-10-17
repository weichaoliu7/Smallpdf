# -*- coding: utf-8 -*-
"""输出正确性验证测试程序。

对 TST_BOOK（test_book.pdf，66MB 扫描书）执行压缩，并用 verify_compression
对每个产物做「结构与内容」双重校验，确认输出 PDF 正确、可读、无空白/黑点。
"""
import os
import sys
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS, ensure_workdir  # noqa: E402

from app.compressor import (
    build_profile, compress_pdf, verify_compression, human_size,
)

SRC = os.path.join(ROOT, "test_book.pdf")
# 产物写入共享工作目录（系统临时目录），供 ordering_check.py 复用
WORKDIR = ensure_workdir()


def run_tier(key):
    dst = os.path.join(WORKDIR, f"out_{key}.pdf")
    t0 = time.time()
    r = compress_pdf(SRC, dst, build_profile(key))
    dt = time.time() - t0
    v = verify_compression(SRC, dst)
    status = "✓ 通过" if v["ok"] else "✗ 失败"
    print(f"[{key:<9}] {human_size(r['original_size'])} -> {human_size(r['final_size'])} "
          f"({r['saved_percent']:5.1f}% 减小, {dt:.0f}s)", flush=True)
    print(f"           校验: {status} | 页数一致={v['pages_match']} | "
          f"校验图片 {v['images_checked']} 张 | 失败 {v['failed_images']} 张 | "
          f"最差 PSNR {v['min_psnr']:.1f} dB | 最差对比度 {v['min_contrast']:.2f}", flush=True)
    if v["errors"]:
        for e in v["errors"][:3]:
            print(f"           错误: {e}", flush=True)
    print("-" * 76, flush=True)
    return v["ok"]


def main():
    if not os.path.exists(SRC):
        print(f"找不到源文件 {SRC}")
        sys.exit(1)
    print(f"源文件: {SRC} ({human_size(os.path.getsize(SRC))})")
    print("=" * 76, flush=True)
    results = {}
    for key in ("high", "balanced", "smallest"):
        results[key] = run_tier(key)
    ok_all = all(results.values())
    print("=" * 76, flush=True)
    print("总结: " + ("全部通过 ✓" if ok_all else "存在失败 ✗"), flush=True)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
