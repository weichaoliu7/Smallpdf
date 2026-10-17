# 测试体系

本目录是 PDF 压缩器的全部验证代码，按**职责**分为四层，统一由 `run_all.py` 驱动。

## 一、目录结构

```
tests/
├── README.md              本文件：测试架构说明
├── _bootstrap.py          统一路径引导（向上定位项目根目录，注入 programs/ 到 sys.path）
├── run_all.py             一键运行全部测试（子进程逐个执行并汇总）
│
├── functional/            功能正确性：压缩结果对不对
│   ├── verify_test.py           三档压缩 + verify_compression 双重自校验
│   ├── e2e_check.py             现场生成 PDF，走完整真实压缩管线
│   ├── mapping_check.py         滑杆 ↔ 前沿位置 position 的映射断言
│   ├── ordering_check.py        三档产物的体积/清晰度单调性
│   ├── verify_all_points.py     44 个前沿点：显示体积 vs 真实压缩体积
│   └── exe_acceptance.py        打包后 exe 的验收（多档位、bounded、小文档、降级路径）
│
├── audit/                 优化质量审计：前沿是不是真的前沿
│   └── pareto_audit.py          L1 非支配筛选 / L2 KKT 等边际 / L3 λ 扫描反例
│
├── performance/           性能与回归：快不快、有没有改坏结果
│   ├── regression_check.py      ★ 零退化回归：与基线指纹逐字段比对
│   ├── baseline_capture.py      采集/刷新基线指纹
│   ├── perf_profile.py          分阶段耗时剖析（cProfile）
│   └── bench_workers.py         并行规模基准（串行/线程/进程 各档对比）
│
├── baselines/             回归基准数据（不随代码改动的黄金样本）
│   └── fingerprint_baseline.json
│
└── reports/               生成的报告（只读产出，由对应脚本写入）
    ├── frontier_44_report.md
    ├── pareto_audit_report.md
    └── optimization_2026-10-11.md
```

## 二、运行方式

```bash
python tests/run_all.py                      # 全部
python tests/run_all.py functional           # 只跑功能测试
python tests/run_all.py --skip exe_acceptance
```

以下脚本耗时较长，默认不纳入一键运行，需要时手动执行：

```bash
python tests/performance/perf_profile.py     # 分阶段耗时剖析
python tests/performance/bench_workers.py    # 并行规模基准
python tests/performance/baseline_capture.py # 刷新基线指纹
```

## 三、分层原则

| 层 | 回答的问题 | 失败意味着什么 |
| --- | --- | --- |
| `functional/` | 输出 PDF 是否正确、可用、与界面一致 | 功能回归，必须修 |
| `audit/` | 优化目标本身是否被正确求解 | 算法退化，必须修 |
| `performance/` | 是否变快、结果是否**逐位不变** | 性能优化改坏了结果，必须修 |
| `baselines/` | — | 黄金样本，仅在有意改变压缩策略时刷新 |

**核心守护测试是 `performance/regression_check.py`**：它把当前引擎的 167 张位图
规划指纹、145 张图的前沿逐点数据、8 个 position 的真实压缩产物，与基线逐字段
比对。任何性能改动只要它仍然通过，就证明压缩结果**零退化**。

## 四、约定

1. 任何测试脚本都通过 `_bootstrap` 取路径，不再自己拼相对路径。
2. 测试只读源文件，产物一律写入临时目录，不污染项目根目录。
   需要跨脚本共享的产物（`verify_test.py` 产出 → `ordering_check.py` 消费）写入
   `_bootstrap.WORKDIR`（默认系统临时目录下的 `pdfc_tests/`）。
3. 报告类输出写入 `REPORTS`（`tests/reports/`），基准数据写入 `BASELINES`。
