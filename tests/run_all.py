# -*- coding: utf-8 -*-
"""一键运行全部测试。

用法：
    python tests/run_all.py              # 全部
    python tests/run_all.py functional   # 只跑某个分组
    python tests/run_all.py --skip exe_acceptance

分组：
    functional   功能正确性（压缩输出、滑杆映射、端到端、exe 验收）
    audit        优化质量审计（帕累托前沿数学性质）
    performance  性能剖析、并行基准、零退化回归

说明：部分用例耗时较长（exe 验收约 8 分钟、44 点校验约 1 分钟），
脚本逐个用子进程执行，任何一个失败都会汇总到末尾并以非零退出码结束。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _bootstrap import ROOT  # noqa: E402

GROUPS = {
    "functional": [
        ("输出正确性校验", "functional/verify_test.py"),
        ("端到端（现场生成 PDF）", "functional/e2e_check.py"),
        ("滑杆↔position 映射", "functional/mapping_check.py"),
        ("档位体积/清晰度排序", "functional/ordering_check.py"),
        ("44 点显示体积 vs 真实体积", "functional/verify_all_points.py"),
        ("打包后 exe 验收", "functional/exe_acceptance.py"),
    ],
    "audit": [
        ("帕累托前沿审计", "audit/pareto_audit.py"),
    ],
    "performance": [
        ("零退化回归校验", "performance/regression_check.py"),
    ],
}

# 默认不跑的长耗时/剖析类脚本（需要时手动执行）
MANUAL = {
    "性能剖析（分阶段耗时）": "performance/perf_profile.py",
    "并行规模基准": "performance/bench_workers.py",
    "基线指纹采集": "performance/baseline_capture.py",
}


def main() -> None:
    argv = sys.argv[1:]
    skip = set()
    if "--skip" in argv:
        i = argv.index("--skip")
        skip = set(argv[i + 1:])
        argv = argv[:i]
    groups = argv or list(GROUPS)

    py = sys.executable
    results = []
    for g in groups:
        if g not in GROUPS:
            print("未知分组：%s（可选：%s）" % (g, " ".join(GROUPS)))
            continue
        print("\n" + "=" * 74)
        print("分组：%s" % g)
        print("=" * 74)
        for name, rel in GROUPS[g]:
            if rel.split("/")[-1] in skip:
                print("  [跳过] %s" % name)
                continue
            path = os.path.join(HERE, rel)
            print("\n  ▶ %s  (%s)" % (name, rel))
            t = time.perf_counter()
            try:
                p = subprocess.run([py, path], cwd=ROOT, timeout=3600,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                out = p.stdout.decode("utf-8", "replace")
                ok = p.returncode == 0
            except subprocess.TimeoutExpired:
                out, ok = "超时（3600 s）", False
            dt = time.perf_counter() - t
            tail = "\n".join(out.strip().split("\n")[-6:])
            print("    耗时 %.1f s  ->  %s" % (dt, "通过" if ok else "失败"))
            if tail:
                print("    " + tail.replace("\n", "\n    "))
            results.append((name, ok))

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    for name, ok in results:
        print("  %s  %s" % ("[OK]  " if ok else "[FAIL]", name))
    bad = sum(1 for _, ok in results if not ok)
    print("\n通过 %d，失败 %d" % (len(results) - bad, bad))
    print("\n手动执行（耗时较长，默认不纳入一键运行）：")
    for k, v in MANUAL.items():
        print("  %-24s python tests/%s" % (k, v))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
