# -*- coding: utf-8 -*-
"""修复验证 2：端到端测试 —— 用真实压缩管线验证「滑杆标签 ↔ 输出 PDF」一一对应。

流程：
1. 现场生成一个含文字页/照片页的小型多页 PDF（无需任何外部素材）；
2. 按界面同样的路径构建配置（App._current_profile），调用 compress_pdf_bounded；
3. 对每个产物做 verify_compression 结构+内容校验，并逐张计算平均 PSNR；
4. 断言：
   - 滑杆 0（画质优先）：体积最大、清晰度最高；
   - 滑杆 50：居中；
   - 滑杆 100（体积优先）：体积最小、清晰度最低；
   - 三档快捷按钮输出顺序 高画质 ≥ 均衡 ≥ 最小体积（体积与清晰度）。
"""
import io
import os
import sys
import tempfile

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS  # noqa: E402

from PIL import Image, ImageDraw, ImageStat


import tkinter as tk

from main import App
from app.compressor import (
    PRESETS,
    _metrics,
    _page_images,
    _reading_reference,
    compress_pdf_bounded,
    human_size,
    verify_compression,
)


def make_sample_pdf(path: str, pages: int = 6) -> None:
    """生成文字页与照片页混合的扫描风格 PDF。"""
    images = []
    for i in range(pages):
        img = Image.new("RGB", (1200, 1600), "white")
        d = ImageDraw.Draw(img)
        if i % 2 == 0:
            # 文字/图表风格：密集横线条 + 矩形块
            for y in range(70, 1550, 26):
                d.line([(80, y), (1120, y)], fill="black", width=3)
            for k in range(6):
                x0 = 90 + k * 180
                d.rectangle([x0, 100, x0 + 120, 240], outline="black", width=4)
        else:
            # 照片风格：彩色渐变 + 随机色块
            for y in range(1600):
                r = int(255 * y / 1600)
                g = int(120 + 100 * i / pages)
                b = 255 - r
                d.line([(0, y), (1200, y)], fill=(r, g, b))
            for k in range(18):
                x0 = (k * 173 + i * 57) % 1100
                y0 = (k * 251 + i * 91) % 1500
                d.ellipse([x0, y0, x0 + 120, y0 + 120],
                          fill=((k * 31) % 255, (k * 67) % 255, (k * 97) % 255))
        images.append(img)
    images[0].save(path, save_all=True, append_images=images[1:])


def mean_psnr(src: str, dst: str) -> float:
    """逐张位图计算平均 PSNR（与 verify_compression 同口径）。"""
    import pikepdf
    vals = []
    with pikepdf.open(src) as a, pikepdf.open(dst) as b:
        for sp, dp in zip(a.pages, b.pages):
            for im_a, im_b in zip(_page_images(sp), _page_images(dp)):
                ref = _reading_reference(im_a)
                std = ImageStat.Stat(ref).stddev[0]
                buf = io.BytesIO()
                im_b.convert("L").save(buf, "PNG")
                psnr, _ = _metrics(buf.getvalue(), ref, std)
                vals.append(psnr)
    return sum(vals) / max(1, len(vals))


def run_one(app: App, src: str, workdir: str, tag: str, slider: float):
    app.slider_var.set(slider)
    profile = app._current_profile()
    dst = os.path.join(workdir, "out_%s.pdf" % tag)
    report = compress_pdf_bounded(src, dst, profile)
    ver = verify_compression(src, dst)
    mpsnr = mean_psnr(src, dst)
    print("  %-14s position=%.2f 体积 %-10s 校验 %s 平均PSNR %.2f dB"
          % (tag, profile.position, human_size(report["final_size"]),
             "通过" if ver["ok"] else "失败", mpsnr), flush=True)
    if not ver["ok"]:
        for e in ver["errors"][:3]:
            print("    ! " + e)
    return {"size": report["final_size"], "psnr": mpsnr, "ok": ver["ok"]}


def main():
    workdir = tempfile.mkdtemp(prefix="pdfc_e2e_")
    src = os.path.join(workdir, "sample.pdf")
    make_sample_pdf(src)
    print("已生成样本 PDF：%s（%s）" % (src, human_size(os.path.getsize(src))), flush=True)

    root = tk.Tk()
    root.withdraw()
    app = App(root)

    failures = []

    def check(name, cond):
        print(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    print("[A] 滑杆 0 / 50 / 100（画质优先 → 体积优先）")
    r0 = run_one(app, src, workdir, "quality", 0.0)
    r50 = run_one(app, src, workdir, "middle", 50.0)
    r100 = run_one(app, src, workdir, "size", 100.0)
    check("三产物全部通过 verify_compression", r0["ok"] and r50["ok"] and r100["ok"])
    check("体积单调不增：画质端 ≥ 中间 ≥ 体积端",
          r0["size"] >= r50["size"] >= r100["size"])
    check("画质端体积严格大于体积端", r0["size"] > r100["size"])
    check("清晰度单调不增：画质端 ≥ 中间 ≥ 体积端",
          r0["psnr"] >= r50["psnr"] >= r100["psnr"])
    check("画质端清晰度严格高于体积端", r0["psnr"] > r100["psnr"])

    print("[B] 三档快捷按钮")
    rh = run_one(app, src, workdir, "high", app._position_to_slider(PRESETS["high"].position))
    rb = run_one(app, src, workdir, "balanced", app._position_to_slider(PRESETS["balanced"].position))
    rs = run_one(app, src, workdir, "smallest", app._position_to_slider(PRESETS["smallest"].position))
    check("三档全部通过校验", rh["ok"] and rb["ok"] and rs["ok"])
    check("体积：高画质 ≥ 均衡 ≥ 最小体积", rh["size"] >= rb["size"] >= rs["size"])
    check("清晰度：高画质 ≥ 均衡 ≥ 最小体积", rh["psnr"] >= rb["psnr"] >= rs["psnr"])

    root.destroy()
    print("=" * 72)
    if failures:
        print("端到端测试存在 %d 项失败：%s" % (len(failures), failures))
        sys.exit(1)
    print("端到端测试全部通过 ✓（界面标签与输出 PDF 严格一一对应）")


if __name__ == "__main__":
    main()
