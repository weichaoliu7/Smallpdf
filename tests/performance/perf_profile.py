# -*- coding: utf-8 -*-
"""性能剖析脚本：定位 PDF 压缩器的计算成本分布（只读，不修改项目文件）。

用法：
    python tests/perf_profile.py [pdf路径]

输出各阶段耗时，用于指导优化。不写入任何项目文件。
"""
from __future__ import annotations

import os
import sys
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS  # noqa: E402

import pikepdf  # noqa: E402
from app import compressor as C  # noqa: E402
from PIL import Image, ImageChops, ImageStat  # noqa: E402

def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "test_book.pdf")
    print("源文件: %s (%.2f MB)" % (src, os.path.getsize(src) / 1048576))

    # ---------------------------------------------------------------- 阶段 0
    hr("阶段 0：打开 PDF 与收集位图引用")
    t = time.perf_counter()
    with pikepdf.open(src) as pdf:
        t_open = time.perf_counter() - t
        t = time.perf_counter()
        _refs, samples = C.collect_image_refs(pdf)
        t_collect = time.perf_counter() - t
        print("pikepdf.open        : %7.2f s" % t_open)
        print("collect_image_refs  : %7.2f s   (唯一位图 %d 张)" % (t_collect, len(samples)))

        # 统计像素规模
        t = time.perf_counter()
        total_px = 0
        total_raw = 0
        dims = []
        for k, o in samples.items():
            try:
                w = int(o.get("/Width")); h = int(o.get("/Height"))
                total_px += w * h
                dims.append(max(w, h))
                total_raw += len(o.read_raw_bytes())
            except Exception:
                pass
        t_meta = time.perf_counter() - t
        print("读取原始字节+尺寸    : %7.2f s" % t_meta)
        print("总像素 %.1f Mpx，原始流合计 %.2f MB，最大边长 max=%d median=%d"
              % (total_px / 1e6, total_raw / 1048576, max(dims),
                 sorted(dims)[len(dims) // 2]))

        # ------------------------------------------------------------ 阶段 1
        hr("阶段 1：_plan_image 全量（候选生成，成本主体）")
        import cProfile
        import pstats
        import io as _io
        pr = cProfile.Profile()
        pr.enable()
        t = time.perf_counter()
        plans = C.precompute_pdf(src)
        t_plan = time.perf_counter() - t
        pr.disable()
        print("precompute_pdf      : %7.2f s" % t_plan)
        s = _io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28)
        print(s.getvalue())

        n_front = sum(1 for p in plans if p["status"] == "frontier")
        n_keep = sum(1 for p in plans if p["status"] == "keep")
        n_fixed = sum(1 for p in plans if p["status"] == "fixed")
        print("plan 状态: frontier=%d keep=%d fixed=%d" % (n_front, n_keep, n_fixed))

    # ---------------------------------------------------------------- 阶段 2
    hr("阶段 2：单图候选生成的内部拆分（抽样 3 张）")
    keys = [p["key"] for p in plans if p["status"] == "frontier"]
    with pikepdf.open(src) as pdf:
        _r, samples = C.collect_image_refs(pdf)
        sample_keys = keys[:1] + keys[len(keys) // 2:len(keys) // 2 + 1] + keys[-1:]
        for idx, k in enumerate(sample_keys):
            obj = samples.get(k)
            if obj is None:
                continue
            t = time.perf_counter()
            img = C._decode_image(obj)
            t_dec = time.perf_counter() - t
            if img is None:
                continue
            t = time.perf_counter()
            gray = C._is_effectively_grayscale(img)
            t_gray = time.perf_counter() - t
            t = time.perf_counter()
            tl = C._is_text_like(img)
            t_text = time.perf_counter() - t

            t = time.perf_counter()
            ref = C._reading_reference(img)
            orig_std = ImageStat.Stat(ref).stddev[0]
            t_ref = time.perf_counter() - t

            # JPEG 编码耗时
            t = time.perf_counter()
            n_jpeg = 0
            for q in range(40, 94, 3):
                d = C._jpeg_bytes(img, q, 0)
                n_jpeg += 1
            t_jpeg = time.perf_counter() - t

            # JPEG 解码 + PSNR 耗时
            t = time.perf_counter()
            for q in range(40, 94, 3):
                d = C._jpeg_bytes(img, q, 0)
                psnr, cr = C._metrics(d, ref, orig_std)
            t_psnr = time.perf_counter() - t

            # resize 耗时
            t = time.perf_counter()
            b60 = img.resize((round(img.size[0] * .6), round(img.size[1] * .6)), Image.LANCZOS)
            b40 = img.resize((round(img.size[0] * .4), round(img.size[1] * .4)), Image.LANCZOS)
            t_resize = time.perf_counter() - t

            # PNG 耗时
            t = time.perf_counter()
            png = C._encode_png_parts(img)
            t_png = time.perf_counter() - t

            print("\n[样本 %d] key=%s size=%s mode=%s text_like=%s gray=%s"
                  % (idx + 1, str(k), img.size, img.mode, tl, gray))
            print("  解码            %6.3f s" % t_dec)
            print("  灰度判定        %6.3f s" % t_gray)
            print("  文字判定        %6.3f s" % t_text)
            print("  参考图+stddev   %6.3f s" % t_ref)
            print("  JPEG编码 x%d    %6.3f s" % (n_jpeg, t_jpeg))
            print("  JPEG编码+PSNR   %6.3f s  (含编码)" % t_psnr)
            print("  resize 0.6/0.4  %6.3f s" % t_resize)
            print("  PNG encode      %6.3f s  -> %s" % (t_png, png["bytes"] if png else None))

    # ---------------------------------------------------------------- 阶段 3
    hr("阶段 3：一次真实压缩（planned 路径）")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="perf_")
    dst = os.path.join(tmp, "out.pdf")
    prof = C.build_profile("balanced")
    t = time.perf_counter()
    r = C.compress_pdf_planned(src, dst, plans, prof)
    t_one = time.perf_counter() - t
    print("compress_pdf_planned : %7.2f s" % t_one)
    print("  产物 %.2f MB, 内部 elapsed_ms=%d" % (r["final_size"] / 1048576, r["elapsed_ms"]))

    # 拆解：只做 apply（不含 save）
    with pikepdf.open(src) as pdf:
        t = time.perf_counter()
        C._apply_plans(pdf, plans, prof)
        t_apply = time.perf_counter() - t
        t = time.perf_counter()
        pdf.save(os.path.join(tmp, "s.pdf"),
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 compress_streams=True, recompress_flate=False,
                 normalize_content=False, linearize=False)
        t_save = time.perf_counter() - t
    print("  _apply_plans      : %7.2f s" % t_apply)
    print("  pdf.save          : %7.2f s" % t_save)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 阶段 4
    hr("阶段 4：聚合前沿 analyze_pdf_frontier（44 次真实压缩）")
    t = time.perf_counter()
    pts, fsz = C.analyze_pdf_frontier(src, n_points=8)
    t_f8 = time.perf_counter() - t
    print("n_points=8          : %7.2f s  -> 单点 %.2f s"
          % (t_f8, (t_f8 - t_plan) / 8))
    print("  (其中 precompute 固定成本 %.2f s)" % t_plan)
    print("  预估 44 点总耗时   : %7.2f s" % (t_plan + (t_f8 - t_plan) / 8 * 44))

    hr("汇总")
    print("precompute_pdf      : %7.2f s" % t_plan)
    print("单次真实压缩        : %7.2f s" % t_one)
    print("  ├ _apply_plans    : %7.2f s" % t_apply)
    print("  └ pdf.save        : %7.2f s" % t_save)
    print("44 点前沿总耗时(估) : %7.2f s" % (t_plan + (t_f8 - t_plan) / 8 * 44))


if __name__ == "__main__":
    main()
