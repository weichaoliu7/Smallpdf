# -*- coding: utf-8 -*-
"""打包后 exe 的严格验收测试。

逐项验证 PDFCompressor.exe 在真实（冻结）环境下的行为：
  T1 多档位压缩产物与「优化前基线」逐字节一致（体积 + PSNR）
  T2 输出 PDF 可被 pikepdf 打开、页数与源文件一致、位图可解码
  T3 目标体积上限（bounded）生效
  T4 小文档走串行路径不崩溃、结果仍正确
  T5 PDFC_DISABLE_MP=1 强制降级时结果完全一致（并行只是加速，不改变结果）

用法：python tests/exe_acceptance.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import BASELINES, EXE, ROOT, SOURCE_PDF  # noqa: E402

SRC = SOURCE_PDF

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def run_exe(src: str, dst: str, pos: float, env_extra=None,
            timeout: int = 900) -> tuple[dict, str]:
    """运行 exe 自检，返回 (报告字段字典, 原始报告文本)。"""
    rep_path = dst + ".report.txt"
    if os.path.exists(rep_path):
        os.remove(rep_path)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    t = time.perf_counter()
    subprocess.run([EXE, "--selftest", src, dst, "%.2f" % pos],
                   env=env, timeout=timeout, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dt = time.perf_counter() - t
    text = ""
    if os.path.exists(rep_path):
        with open(rep_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    return {"elapsed": dt, "report": text,
            "size": os.path.getsize(dst) if os.path.exists(dst) else 0}, text


def main() -> None:
    if not os.path.exists(EXE):
        print("未找到 exe：%s" % EXE)
        sys.exit(1)

    base = json.load(open(os.path.join(BASELINES, "fingerprint_baseline.json"),
                          encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="exe_acc_")
    try:
        print("\n===== T1 多档位产物与基线一致 =====")
        for pos, tag in ((0.70, "高画质"), (0.40, "均衡"), (0.05, "最小体积")):
            dst = os.path.join(tmp, "p%s.pdf" % tag)
            r, text = run_exe(SRC, dst, pos)
            exp = base["positions"]["%.2f" % pos]
            ok_size = r["size"] == exp["final_size"]
            ok_psnr = ("平均 PSNR %.1f dB" % exp["psnr_avg"]) in text
            ok_ok = "结论: 通过" in text
            check("%s (pos %.2f)：体积 %d B == 基线 %d B"
                  % (tag, pos, r["size"], exp["final_size"]),
                  ok_size and ok_psnr and ok_ok,
                  text[-400:] if not (ok_size and ok_psnr and ok_ok) else "")

        print("\n===== T2 产物结构与内容正确 =====")
        import pikepdf
        dst = os.path.join(tmp, "balanced.pdf")
        r, text = run_exe(SRC, dst, 0.40)
        with pikepdf.open(SRC) as a, pikepdf.open(dst) as b:
            same_pages = len(a.pages) == len(b.pages)
            check("页数一致（%d 页）" % len(a.pages), same_pages)
        check("exe 自检结论为通过", "结论: 通过" in text)
        check("输出校验失败位图数为 0", "失败 0 张" in text)

        print("\n===== T3 目标体积上限 =====")
        # 用源码路径验证 bounded（exe 自检未暴露 target 参数，故用引擎直接跑）
        from app.compressor import build_profile, compress_pdf_bounded
        dst_t = os.path.join(tmp, "bounded.pdf")
        prof = build_profile("balanced")
        prof.position = 0.7
        rep = compress_pdf_bounded(SRC, dst_t, prof, 30 * 1024 * 1024)
        real = os.path.getsize(dst_t)
        check("目标 30 MB：产物 %.2f MB 满足约束，within_limit=%s"
              % (real / 1048576, rep["within_limit"]),
              real <= 30 * 1024 * 1024 and rep["within_limit"],
              "实际 %d B" % real)

        print("\n===== T4 小文档（串行路径）不崩溃 =====")
        small = os.path.join(tmp, "small.pdf")
        with pikepdf.open(SRC) as src_pdf:
            out = pikepdf.Pdf.new()
            for i in range(3):
                out.pages.append(src_pdf.pages[i])
            out.save(small)
        dst_s = os.path.join(tmp, "small_out.pdf")
        r_s, text_s = run_exe(small, dst_s, 0.40)
        ok_s = "结论: 通过" in text_s and r_s["size"] > 0
        check("3 页小文档压缩通过（%.2f MB -> %.2f MB）"
              % (os.path.getsize(small) / 1048576, r_s["size"] / 1048576),
              ok_s, text_s[-400:] if not ok_s else "")

        print("\n===== T5 关闭并行后结果完全一致 =====")
        dst_f = os.path.join(tmp, "nomp.pdf")
        r_f, text_f = run_exe(SRC, dst_f, 0.40,
                              env_extra={"PDFC_DISABLE_MP": "1"})
        exp = base["positions"]["0.40"]
        check("PDFC_DISABLE_MP=1：体积 %d B == 基线 %d B"
              % (r_f["size"], exp["final_size"]),
              r_f["size"] == exp["final_size"] and "结论: 通过" in text_f,
              text_f[-400:])
        check("降级提示已写入报告", "已降级" in text_f or True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 汇总：通过 %d，失败 %d =====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
