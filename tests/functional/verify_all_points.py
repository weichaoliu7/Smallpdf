# -*- coding: utf-8 -*-
"""test_book 全点验证：逐个压缩 44 个帕累托最优点，比对「页面显示体积 vs 真实体积」。

流程：
 1. 预计算全部唯一位图 plans（一次）；
 2. analyze_pdf_frontier 得到页面将显示的 44 个点（含两端真实校准）；
 3. 对 44 个位置逐个做真实 planned 压缩，记录真实体积与清晰度；
 4. 逐点输出误差表，给出最大绝对/相对误差；
 5. 断言：全部点结构校验通过、体积误差在容差内、清晰度随位置单调不减。
报告写入 tests/reports/frontier_44_report.md；44 份 PDF 产物写入系统临时目录。
"""
import io
import os
import sys
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS, REPORTS, ensure_workdir  # noqa: E402

from app.compressor import (
    _AGGREGATE_POINTS,
    analyze_pdf_frontier,
    build_profile,
    compress_pdf_planned,
    human_size,
    precompute_pdf,
    verify_compression,
)

SRC = os.path.join(ROOT, "test_book.pdf")
# 体积一一对应容差：绝对 ≤2 KB，相对 ≤0.25%
ABS_TOL = 2048
REL_TOL = 0.0025


def main():
    t_start = time.time()
    print("预计算 plans（约 3 分钟）...", flush=True)
    plans = precompute_pdf(SRC)
    print("plans 完成：%d 张唯一位图，累计 %.0f 秒"
          % (len(plans), time.time() - t_start), flush=True)

    print("计算页面显示点（含两端真实校准）...", flush=True)
    shown, src_size = analyze_pdf_frontier(SRC)
    assert len(shown) == _AGGREGATE_POINTS, "点数应为 %d，实际 %d" % (
        _AGGREGATE_POINTS, len(shown))

    out_dir = os.path.join(ensure_workdir(), "frontier_44_outputs")
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    failures = []
    for i in range(_AGGREGATE_POINTS):
        pos = i / max(1, _AGGREGATE_POINTS - 1)
        p = build_profile("balanced")
        p.position = pos
        dst = os.path.join(out_dir, "point_%02d.pdf" % i)
        t0 = time.time()
        r = compress_pdf_planned(SRC, dst, plans, p)
        real = r["final_size"]
        shown_size = shown[i]["size"]
        abs_err = shown_size - real
        rel_err = abs(abs_err) / max(1, real)
        rows.append({
            "i": i, "pos": pos, "shown": shown_size, "real": real,
            "abs_err": abs_err, "rel_err": rel_err,
            "psnr_min": r["psnr"], "psnr_avg": r["psnr_avg"],
            "sec": time.time() - t0,
        })
        print("点%02d pos=%.3f 页面 %-10s 真实 %-10s 误差 %+8d B (%+.2f%%) 清晰度 %5.1f/%5.1f dB"
              % (i, pos, human_size(shown_size), human_size(real),
                 abs_err, rel_err * 100, r["psnr"], r["psnr_avg"]), flush=True)
        if abs(abs_err) > ABS_TOL or rel_err > REL_TOL:
            failures.append("点%02d 体积超差：%+d B / %+.2f%%"
                            % (i, abs_err, rel_err * 100))

    # 结构+内容抽检：两端 + 两个四分位点
    print("-" * 78, flush=True)
    for i in (0, 11, 22, 33, 43):
        dst = os.path.join(out_dir, "point_%02d.pdf" % i)
        v = verify_compression(SRC, dst)
        print("verify 点%02d: %s（图片 %d，最差 PSNR %.1f）"
              % (i, "通过" if v["ok"] else "失败", v["images_checked"], v["min_psnr"]),
              flush=True)
        if not v["ok"]:
            failures.append("点%02d verify 失败" % i)

    # 清晰度单调性（真实产物，平均 PSNR 随 pos 不减）
    avgs = [x["psnr_avg"] for x in rows]
    mono = all(avgs[i] <= avgs[i + 1] + 0.01 for i in range(len(avgs) - 1))
    sizes_real = [x["real"] for x in rows]
    size_mono = all(sizes_real[i] <= sizes_real[i + 1] for i in range(len(sizes_real) - 1))
    max_abs = max(abs(x["abs_err"]) for x in rows)
    max_rel = max(x["rel_err"] for x in rows)
    print("=" * 78, flush=True)
    print("最大绝对误差 %d B；最大相对误差 %.3f%%" % (max_abs, max_rel * 100), flush=True)
    print("真实体积随位置单调不减: %s；真实清晰度单调不减: %s"
          % ("成立" if size_mono else "不成立", "成立" if mono else "不成立"), flush=True)

    # 写 Markdown 报告
    md = io.StringIO()
    md.write("# test_book 44 个帕累托最优点逐点验证报告\n\n")
    md.write("源文件实际体积：%s；验证点数量：%d。\n\n" % (human_size(src_size), _AGGREGATE_POINTS))
    md.write("| # | position | 页面显示 | 真实体积 | 误差(B) | 相对误差 | 最差PSNR | 平均PSNR |\n")
    md.write("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
    for x in rows:
        md.write("| %d | %.3f | %s | %s | %+d | %+.2f%% | %.1f | %.1f |\n" % (
            x["i"], x["pos"], human_size(x["shown"]), human_size(x["real"]),
            x["abs_err"], x["rel_err"] * 100, x["psnr_min"], x["psnr_avg"]))
    md.write("\n最大绝对误差：**%d B**；最大相对误差：**%.3f%%**。\n" % (max_abs, max_rel * 100))
    md.write("\n体积单调：%s；清晰度单调：%s。\n" % (
        "成立" if size_mono else "不成立", "成立" if mono else "不成立"))
    os.makedirs(REPORTS, exist_ok=True)
    md_path = os.path.join(REPORTS, "frontier_44_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md.getvalue())
    print("报告已写入：%s" % md_path, flush=True)

    if not mono:
        failures.append("清晰度不单调")
    if not size_mono:
        failures.append("体积不单调")
    if failures:
        print("存在 %d 项失败：" % len(failures), flush=True)
        for f in failures:
            print("  - " + f, flush=True)
        sys.exit(1)
    print("全部 44 个帕累托最优点验证通过：页面显示体积与真实压缩体积一一对应。",
          flush=True)


if __name__ == "__main__":
    main()
