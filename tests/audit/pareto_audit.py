# -*- coding: utf-8 -*-
"""帕累托前沿审计：论证「等索引聚合」是否构成真正的（文档级）帕累托前沿。

三个层次的检验：
  L1 单图层面：候选集内的非支配筛选是否正确、前沿是否完整（是否被抽稀）。
  L2 聚合层面：文档级目标的可分结构要求「各图边际替代率相等」（KKT 条件）。
     检验等索引聚合是否满足该条件；若不满足，则在数学上必然存在支配它的分配。
  L3 实证层面：用 λ-拉格朗日扫描构造反例分配，并做真实压缩，检验是否
     在真实体积 ≤ 基准体积的前提下得到更高的平均清晰度（构成支配）。

用法：python tests/pareto_audit.py
"""
import io
import math
import os
import statistics
import sys
import tempfile
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS, REPORTS  # noqa: E402

import app.compressor as C
from app.compressor import (
    build_profile, compress_pdf_planned, human_size, precompute_pdf,
)

SRC = os.path.join(ROOT, "test_book.pdf")
ORIG_SELECT = C._plan_selected


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def is_variable(pl):
    return pl.get("status") == "frontier"


def fixed_bytes(plans):
    """不随 position 变化的字节（keep / fixed 两类图）。"""
    tot = 0
    for pl in plans:
        if pl["status"] == "keep":
            tot += len(pl["raw"])
        elif pl["status"] == "fixed":
            tot += pl["cand"]["size"]
    return tot


def fixed_psnr_mass(plans):
    """固定图的 PSNR 累加值与数量（这部分对比较是常数）。"""
    s, n = 0.0, 0
    for pl in plans:
        if pl["status"] == "keep":
            s += 99.0
            n += 1
        elif pl["status"] == "fixed":
            s += float(pl["cand"].get("psnr") or 99.0)
            n += 1
    return s, n


def equal_index_map(plans, p):
    """等索引聚合：所有图共用同一个 position，各自按自己的前沿长度取索引。"""
    m = {}
    for pl in plans:
        if is_variable(pl):
            n = len(pl["frontier"])
            m[pl["key"]] = int(round(p * (n - 1)))
    return m


def lambda_index_map(plans, lam):
    """λ-拉格朗日聚合：每张图独立最大化 psnr - λ·size（等价于等边际替代率）。"""
    m = {}
    for pl in plans:
        if is_variable(pl):
            fr = pl["frontier"]
            m[pl["key"]] = max(range(len(fr)),
                               key=lambda i: fr[i]["psnr"] - lam * fr[i]["size"])
    return m


def estimate(plans, idx_map):
    """在「估计平面」上评估一个分配方案：返回 (总字节, 全体平均PSNR, 可变图平均PSNR)。"""
    tot, vs, vn, fs, fn = 0, 0.0, 0, 0.0, 0
    for pl in plans:
        if is_variable(pl):
            c = pl["frontier"][idx_map[pl["key"]]]
            tot += c["size"]
            vs += c["psnr"]
            vn += 1
        elif pl["status"] == "fixed":
            tot += pl["cand"]["size"]
            fs += float(pl["cand"].get("psnr") or 99.0)
            fn += 1
        else:
            tot += len(pl["raw"])
            fs += 99.0
            fn += 1
    return tot, (vs + fs) / max(1, vn + fn), vs / max(1, vn)


def dominates(a, b):
    """a 支配 b：体积不大于 且 清晰度不低于，且至少一个严格更优。"""
    sa, pa = a
    sb, pb = b
    return (sa <= sb and pa >= pb) and (sa < sb or pa > pb)


def real_run(plans, idx_map, tag, out_dir):
    """用指定的 per-image 索引跑一次真实 planned 压缩。"""
    def patched(plan, position):
        if is_variable(plan) and plan["key"] in idx_map:
            fr = plan["frontier"]
            return fr[max(0, min(len(fr) - 1, idx_map[plan["key"]]))]
        return ORIG_SELECT(plan, position)

    C._plan_selected = patched
    try:
        prof = build_profile("balanced")
        prof.position = 0.5
        dst = os.path.join(out_dir, "%s.pdf" % tag)
        t0 = time.time()
        r = compress_pdf_planned(SRC, dst, plans, prof)
        return {
            "tag": tag,
            "size": int(r["final_size"]),
            "psnr_avg": round(float(r["psnr_avg"]), 2),
            "psnr_min": round(float(r["psnr"]), 2),
            "sec": round(time.time() - t0, 1),
        }
    finally:
        C._plan_selected = ORIG_SELECT


# ---------------------------------------------------------------------------
# L1：单图前沿完整性
# ---------------------------------------------------------------------------
def audit_l1(plans):
    lens = [len(pl["frontier"]) for pl in plans if is_variable(pl)]
    mono_ok, nonmono = True, 0
    for pl in plans:
        if not is_variable(pl):
            continue
        fr = pl["frontier"]
        for a, b in zip(fr, fr[1:]):
            if b["size"] < a["size"] or b["psnr"] < a["psnr"] - 1e-9:
                mono_ok = False
                nonmono += 1
                break
    return {
        "var_images": len(lens),
        "frontier_len_min": min(lens) if lens else 0,
        "frontier_len_max": max(lens) if lens else 0,
        "frontier_len_mean": round(statistics.mean(lens), 2) if lens else 0,
        "monotone_ok": mono_ok,
        "monotone_violations": nonmono,
        "resample_triggered": any(l > C._FRONTIER_POINTS for l in lens),
    }


# ---------------------------------------------------------------------------
# L2：边际替代率离散度（KKT 等边际条件检验）
# ---------------------------------------------------------------------------
def audit_l2(plans, p):
    """在等索引方案 p 下，统计各图选中点处的边际替代率 dPSNR/dSize 的离散度。

    若文档级分配是帕累托最优的，所有可变图在该点的边际替代率必须相等（或落在
    离散前沿的同一个「斜率区间」）。离散度越大，说明资源错配越严重。
    """
    slopes = []
    for pl in plans:
        if not is_variable(pl):
            continue
        fr = pl["frontier"]
        i = int(round(p * (len(fr) - 1)))
        lo = fr[max(0, i - 1)]
        hi = fr[min(len(fr) - 1, i + 1)]
        dsize = hi["size"] - lo["size"]
        dpsnr = hi["psnr"] - lo["psnr"]
        if dsize > 0 and dpsnr > 0:
            slopes.append(dpsnr / dsize)   # dB / byte
    if not slopes:
        return {}
    slopes.sort()
    n = len(slopes)
    return {
        "position": p,
        "n": n,
        "p10": slopes[int(0.10 * (n - 1))],
        "median": slopes[n // 2],
        "p90": slopes[int(0.90 * (n - 1))],
        "ratio_p90_p10": (slopes[int(0.90 * (n - 1))] /
                          max(1e-18, slopes[int(0.10 * (n - 1))])),
    }


# ---------------------------------------------------------------------------
# L3：λ-扫描反例搜索
# ---------------------------------------------------------------------------
def audit_l3(plans, n_probe=13):
    """在估计平面上对比「等索引前沿」与「λ 前沿」，并对可比点做真实压缩验证。"""
    base = []
    for i in range(n_probe):
        p = i / (n_probe - 1)
        m = equal_index_map(plans, p)
        sz, pall, pvar = estimate(plans, m)
        base.append({"kind": "equal-index", "p": p, "size": sz,
                     "psnr_all": pall, "psnr_var": pvar, "map": m})

    lam_curve = []
    seen = set()
    for k in range(120):
        lam = 10 ** (-7.0 + 5.0 * k / 119.0)
        m = lambda_index_map(plans, lam)
        sz, pall, pvar = estimate(plans, m)
        sig = tuple(sorted(m.items()))
        if sig in seen:
            continue
        seen.add(sig)
        lam_curve.append({"kind": "lambda", "lam": lam, "size": sz,
                          "psnr_all": pall, "psnr_var": pvar, "map": m})

    # 在估计平面上统计支配关系
    ei_pts = [(b["size"], b["psnr_all"]) for b in base]
    lam_pts = [(l["size"], l["psnr_all"]) for l in lam_curve]
    dom_count = 0
    dom_examples = []
    for b, lb in zip(base, lam_curve):
        pass
    for b in base:
        bp = (b["size"], b["psnr_all"])
        hit = [l for l in lam_curve if dominates((l["size"], l["psnr_all"]), bp)]
        if hit:
            dom_count += 1
            best = max(hit, key=lambda l: (l["psnr_all"], -l["size"]))
            dom_examples.append((b, best, lam_pts))
    return base, lam_curve, dom_count, dom_examples


def main():
    t0 = time.time()
    print("预计算 plans ...", flush=True)
    plans = precompute_pdf(SRC)
    n_total = len(plans)
    print("完成：%d 张唯一位图，耗时 %.0f 秒" % (n_total, time.time() - t0), flush=True)

    out = io.StringIO()
    out.write("# 帕累托前沿审计报告（test_book.pdf）\n\n")
    out.write("源文件：%s；唯一位图 %d 张。\n\n" % (human_size(os.path.getsize(SRC)), n_total))

    # ---- L1 ----
    l1 = audit_l1(plans)
    print("L1 单图层面：", l1, flush=True)
    out.write("## L1 单图层面（候选集内的非支配筛选）\n\n")
    out.write("| 项目 | 值 |\n| --- | --- |\n")
    out.write("| 参与前沿选图的可变位图 | %d |\n" % l1["var_images"])
    out.write("| 单图前沿点数 min/mean/max | %d / %.2f / %d |\n"
              % (l1["frontier_len_min"], l1["frontier_len_mean"], l1["frontier_len_max"]))
    out.write("| 前沿 (体积↑, 清晰度↑) 双调性 | %s |\n"
              % ("成立" if l1["monotone_ok"] else "违反 %d 例" % l1["monotone_violations"]))
    out.write("| 是否被 _resample_frontier 抽稀 | %s |\n"
              % ("是" if l1["resample_triggered"] else "否（点数未超过 44）"))
    out.write("\n")

    # ---- L2 ----
    out.write("## L2 边际替代率离散度（KKT 等边际条件）\n\n")
    out.write("文档级帕累托最优要求：所有位图在选中点处的边际替代率 dPSNR/dSize 相等。\n\n")
    out.write("| position | 样本数 | P10 (dB/MB) | 中位数 | P90 | P90/P10 |\n")
    out.write("| --- | --- | --- | --- | --- | --- |\n")
    for p in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95):
        r = audit_l2(plans, p)
        if not r:
            continue
        out.write("| %.2f | %d | %.3f | %.3f | %.3f | **%.1f×** |\n"
                  % (r["position"], r["n"], r["p10"] * 1e6, r["median"] * 1e6,
                     r["p90"] * 1e6, r["ratio_p90_p10"]))
        print("L2 p=%.2f  P90/P10 = %.1fx" % (p, r["ratio_p90_p10"]), flush=True)
    out.write("\n")

    # ---- L3 ----
    base, lam_curve, dom_count, dom_examples = audit_l3(plans)
    print("L3 估计平面：等索引点被 λ 点支配的数量 = %d / %d"
          % (dom_count, len(base)), flush=True)

    out.write("## L3 估计平面：λ-拉格朗日扫描 vs 等索引聚合\n\n")
    out.write("等索引点被 λ 方案支配的数量：**%d / %d**\n\n" % (dom_count, len(base)))
    out.write("| 等索引 position | 估计体积 | 平均PSNR | 最优 λ 方案体积 | 平均PSNR | Δ体积 | Δ清晰度 |\n")
    out.write("| --- | --- | --- | --- | --- | --- | --- |\n")
    for b, best, _ in dom_examples:
        out.write("| %.3f | %s | %.2f | %s | %.2f | %+.2f%% | %+.2f dB |\n"
                  % (b["p"], human_size(b["size"]), b["psnr_all"],
                     human_size(best["size"]), best["psnr_all"],
                     (best["size"] - b["size"]) / b["size"] * 100,
                     best["psnr_all"] - b["psnr_all"]))
    out.write("\n")

    # ---- L3 真实压缩验证：挑「估计体积最接近」的配对 ----
    out.write("## L3 真实压缩验证（同一条 planned 压缩路径）\n\n")
    pairs = []
    for b in base:
        cand = [l for l in lam_curve if l["size"] <= b["size"] * 1.02]
        if not cand:
            continue
        best = max(cand, key=lambda l: l["psnr_all"])
        pairs.append((b, best))
    pairs.sort(key=lambda t: t[1]["psnr_all"] - t[0]["psnr_all"], reverse=True)
    pairs = pairs[:6]

    tmp = tempfile.mkdtemp(prefix="pdfc_audit_")
    out.write("| 配对 | 方案 | 真实体积 | 平均PSNR | 最差PSNR | Δ体积 | Δ平均清晰度 | 结论 |\n")
    out.write("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
    real_rows = []
    for i, (b, l) in enumerate(pairs):
        rb = real_run(plans, b["map"], "base%02d" % i, tmp)
        rl = real_run(plans, l["map"], "lam%02d" % i, tmp)
        d_sz = rl["size"] - rb["size"]
        d_p = rl["psnr_avg"] - rb["psnr_avg"]
        verdict = ("λ 方案支配" if dominates((rl["size"], rl["psnr_avg"]),
                                            (rb["size"], rb["psnr_avg"]))
                   else ("λ 方案不劣（体积更小、清晰度持平）"
                         if rl["size"] < rb["size"] and abs(d_p) < 0.05
                         else "未构成支配"))
        out.write("| %d | 等索引 p=%.3f | %s | %.2f | %.2f | — | — | — |\n"
                  % (i + 1, b["p"], human_size(rb["size"]), rb["psnr_avg"], rb["psnr_min"]))
        out.write("| %d | λ=%.2e | %s | %.2f | %.2f | %+.2f%% | %+.2f dB | **%s** |\n"
                  % (i + 1, l["lam"], human_size(rl["size"]), rl["psnr_avg"],
                     rl["psnr_min"], d_sz / rb["size"] * 100, d_p, verdict))
        print("配对%d: base %.2f dB / %s  vs  λ %.2f dB / %s  →  %s"
              % (i + 1, rb["psnr_avg"], human_size(rb["size"]),
                 rl["psnr_avg"], human_size(rl["size"]), verdict), flush=True)
        real_rows.append((rb, rl, verdict))

    out.write("\n")
    os.makedirs(REPORTS, exist_ok=True)
    md = os.path.join(REPORTS, "pareto_audit_report.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(out.getvalue())
    print("报告已写入：%s" % md, flush=True)


if __name__ == "__main__":
    main()
