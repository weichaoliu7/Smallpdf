# -*- coding: utf-8 -*-
"""采集压缩结果指纹，作为「零退化」回归校验的基准数据。

对 test_book.pdf 执行：
  1. precompute_pdf 全量规划（记录每张图的帕累托前沿：体积/清晰度/对比度/名称/尺寸）
  2. 多个 position 下的真实压缩（记录产物体积、平均/最差 PSNR）
  3. 记录耗时

输出 JSON 到 tests/baselines/fingerprint_<tag>.json。
日常校验请直接运行 performance/regression_check.py；只有在确认引擎应当产生
**新**的基准结果时（例如有意调整压缩策略），才需要用本脚本刷新基线。

用法：python tests/performance/baseline_capture.py <tag> [pdf路径]
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

def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "before"
    src = sys.argv[2] if len(sys.argv) > 2 else SOURCE_PDF

    fp: dict = {
        "tag": tag,
        "src": os.path.basename(src),
        "src_size": os.path.getsize(src),
        "positions": {},
        "plans": {},
        "timing": {},
    }

    t = time.perf_counter()
    plans = C.precompute_pdf(src)
    fp["timing"]["precompute_s"] = round(time.perf_counter() - t, 3)

    # 逐图前沿指纹：体积 / 清晰度 / 对比度 / 编码名 / 像素尺寸
    for p in plans:
        key = str(p.get("key"))
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
                for c in p["frontier"]
            ]
        elif p.get("status") == "fixed":
            cnd = p.get("cand") or {}
            entry["fixed"] = [int(cnd.get("size", 0)), str(cnd.get("name")),
                              int(cnd.get("width", 0)), int(cnd.get("height", 0))]
        fp["plans"][key] = entry

    # 多个 position 下的真实压缩结果
    tmp = tempfile.mkdtemp(prefix="fp_")
    try:
        for pos in (0.0, 0.05, 0.2, 0.4, 0.6, 0.7, 0.85, 1.0):
            prof = C.build_profile("balanced")
            prof.position = pos
            dst = os.path.join(tmp, "p%s.pdf" % str(pos).replace(".", "_"))
            t = time.perf_counter()
            r = C.compress_pdf_planned(src, dst, plans, prof)
            dt = time.perf_counter() - t
            fp["positions"]["%.2f" % pos] = {
                "final_size": int(r["final_size"]),
                "psnr_min": r["psnr"],
                "psnr_avg": r["psnr_avg"],
                "kept": r["kept"],
                "lossy": r["lossy_recompressed"],
                "lossless": r["lossless_recompressed"],
                "downsampled": r["downsampled"],
                "gray": r["gray_converted"],
                "fallback": r["fallback_kept_original"],
                "compress_s": round(dt, 3),
            }
            print("pos=%.2f -> %.3f MB  psnr_avg=%.2f  (%.2f s)"
                  % (pos, r["final_size"] / 1048576, r["psnr_avg"], dt))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    out = os.path.join(BASELINES, "fingerprint_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, sort_keys=True)
    print("\nprecompute: %.2f s" % fp["timing"]["precompute_s"])
    print("指纹已写入: %s (%d 张图)" % (out, len(fp["plans"])))


if __name__ == "__main__":
    main()
