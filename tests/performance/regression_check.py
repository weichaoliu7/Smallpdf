# -*- coding: utf-8 -*-
"""结果零退化回归校验（本项目的核心守护测试）。

把当前引擎对 test_book.pdf 的完整输出指纹，与 ``baselines/fingerprint_baseline.json``
（优化前基线）逐字段比对：

  - 167 张位图的规划状态、原始流长度、色彩空间、灰度判定；
  - 145 张前沿图的**每一个**帕累托前沿点（体积 / 清晰度 / 对比度 / 编码名 / 尺寸）；
  - 8 个代表性 position 下的真实压缩产物体积、平均 PSNR、最差 PSNR。

全部相等即证明：任何性能改动都没有改变压缩结果（体积与清晰度零退化）。
同时打印规划耗时，用于跟踪性能。

用法：python tests/performance/regression_check.py
退出码 0 = 完全一致。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import BASELINES, ROOT, SOURCE_PDF  # noqa: E402

from app import compressor as C  # noqa: E402

POSITIONS = (0.0, 0.05, 0.2, 0.4, 0.6, 0.7, 0.85, 1.0)


def current_fingerprint(src: str) -> dict:
    fp: dict = {"plans": {}, "positions": {}, "timing": {}}
    t = time.perf_counter()
    plans = C.precompute_pdf(src)
    fp["timing"]["precompute_s"] = round(time.perf_counter() - t, 3)

    for p in plans:
        entry = {
            "status": p.get("status"),
            "raw_len": len(p.get("raw") or b""),
            "w": p.get("width"), "h": p.get("height"),
            "cs": p.get("colorspace"),
            "gray": bool(p.get("gray_converted")),
        }
        if p.get("status") == "frontier":
            entry["frontier"] = [
                [int(c["size"]), round(float(c["psnr"]), 6),
                 round(float(c.get("contrast", 1.0)), 6), str(c["name"]),
                 int(c.get("width", 0)), int(c.get("height", 0))]
                for c in p["frontier"]]
        elif p.get("status") == "fixed":
            cnd = p.get("cand") or {}
            entry["fixed"] = [int(cnd.get("size", 0)), str(cnd.get("name")),
                              int(cnd.get("width", 0)), int(cnd.get("height", 0))]
        fp["plans"][str(p.get("key"))] = entry

    tmp = tempfile.mkdtemp(prefix="regress_")
    try:
        for pos in POSITIONS:
            prof = C.build_profile("balanced")
            prof.position = pos
            dst = os.path.join(tmp, "p%s.pdf" % str(pos).replace(".", "_"))
            r = C.compress_pdf_planned(src, dst, plans, prof)
            fp["positions"]["%.2f" % pos] = {
                "final_size": int(r["final_size"]),
                "psnr_min": r["psnr"], "psnr_avg": r["psnr_avg"],
                "kept": r["kept"], "lossy": r["lossy_recompressed"],
                "lossless": r["lossless_recompressed"],
                "downsampled": r["downsampled"], "gray": r["gray_converted"],
                "fallback": r["fallback_kept_original"],
            }
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return fp


def main() -> None:
    base_path = os.path.join(BASELINES, "fingerprint_baseline.json")
    if not os.path.exists(base_path):
        print("缺少基线文件：%s" % base_path)
        sys.exit(2)
    base = json.load(open(base_path, encoding="utf-8"))

    print("源文件: %s" % SOURCE_PDF)
    cur = current_fingerprint(SOURCE_PDF)

    print("\n--- 位图规划指纹 ---")
    bad = [k for k in base["plans"] if base["plans"][k] != cur["plans"].get(k)]
    print("  比对 %d 张位图：差异 %d 处" % (len(base["plans"]), len(bad)))
    for k in bad[:5]:
        print("    ! %s\n      基线 %s\n      当前 %s" % (k, base["plans"][k], cur["plans"].get(k)))

    print("\n--- 真实压缩产物 ---")
    pbad = 0
    for k in sorted(base["positions"]):
        a, b = base["positions"][k], cur["positions"].get(k, {})
        same = (a["final_size"] == b.get("final_size")
                and a["psnr_avg"] == b.get("psnr_avg")
                and a["psnr_min"] == b.get("psnr_min"))
        if not same:
            pbad += 1
        print("  position %s  %10d B / 平均 %5.1f dB / 最差 %5.1f dB   %s"
              % (k, b.get("final_size", 0), b.get("psnr_avg", 0),
                 b.get("psnr_min", 0), "一致" if same else "**不一致**"))

    print("\n--- 性能 ---")
    bt = base["timing"]["precompute_s"]
    ct = cur["timing"]["precompute_s"]
    print("  规划耗时：基线 %.2f s -> 当前 %.2f s（%.2fx，减少 %.1f%%）"
          % (bt, ct, bt / ct, 100 * (1 - ct / bt)))

    ok = not bad and not pbad
    print("\n结论：%s" % ("结果完全一致，零退化 ✓" if ok else "存在退化 ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
