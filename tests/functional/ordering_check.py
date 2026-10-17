# -*- coding: utf-8 -*-
"""真实产物排序核验：对 verify_test.py 产出的 out_{high,balanced,smallest}.pdf
（位于共享工作目录 _bootstrap.WORKDIR，默认系统临时目录 pdfc_tests/）
逐张位图计算 PSNR（相对源文件 test_book.pdf），报告体积与清晰度排序。

用途：证明压缩引擎本身的语义是否与档位名称一致
（高画质 应体积最大、清晰度最高；最小体积 应体积最小）。
"""
import io
import os
import sys

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS, ensure_workdir  # noqa: E402

from PIL import Image, ImageStat


import pikepdf

from app.compressor import _metrics, _page_images, _reading_reference, human_size

SRC = os.path.join(ROOT, "test_book.pdf")
# 消费 verify_test.py 在同一共享工作目录产出的 out_<tier>.pdf
WORKDIR = ensure_workdir()
TIERS = ("high", "balanced", "smallest")


def per_image_psnrs(path):
    """返回产物中每张位图相对源图的 PSNR 列表（按页/资源顺序对齐）。"""
    psnrs = []
    with pikepdf.open(SRC) as src, pikepdf.open(path) as dst:
        for sp, dp in zip(src.pages, dst.pages):
            a_imgs = _page_images(sp)
            b_imgs = _page_images(dp)
            for a, b in zip(a_imgs, b_imgs):
                ref = _reading_reference(a)
                std = ImageStat.Stat(ref).stddev[0]
                buf = io.BytesIO()
                b.convert("L").save(buf, "PNG")
                psnr, _cr = _metrics(buf.getvalue(), ref, std)
                psnrs.append(psnr)
    return psnrs


def main():
    print("源文件: %s (%s)" % (os.path.basename(SRC), human_size(os.path.getsize(SRC))), flush=True)
    rows = []
    for key in TIERS:
        path = os.path.join(WORKDIR, "out_%s.pdf" % key)
        if not os.path.exists(path):
            print("缺少产物 %s —— 请先运行 tests/functional/verify_test.py" % path, flush=True)
            sys.exit(1)
        size = os.path.getsize(path)
        psnrs = per_image_psnrs(path)
        mean_psnr = sum(psnrs) / len(psnrs)
        rows.append((key, size, min(psnrs), mean_psnr, len(psnrs)))
        print("[%-8s] 体积 %s | 图片 %d 张 | 最差 PSNR %.2f dB | 平均 PSNR %.2f dB"
              % (key, human_size(size), len(psnrs), min(psnrs), mean_psnr), flush=True)

    # 排序断言：体积 high > balanced > smallest；平均清晰度同序
    sizes = [r[1] for r in rows]
    means = [r[3] for r in rows]
    print("-" * 72, flush=True)
    print("体积降序(high>balanced>smallest): %s"
      % ("成立" if sizes[0] > sizes[1] > sizes[2] else "不成立"), flush=True)
    print("平均清晰度降序(high>=balanced>=smallest): %s"
      % ("成立" if means[0] >= means[1] >= means[2] else "不成立"), flush=True)


if __name__ == "__main__":
    main()
