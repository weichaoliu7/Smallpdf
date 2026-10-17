# -*- coding: utf-8 -*-
"""修复验证 1：界面「滑杆 ↔ 前沿位置 position」一一对应测试（无压缩，秒级）。

覆盖：
1. 滑杆两端：左端 0（标签「画质优先」）→ position 1.0；右端 100（标签「体积优先」）→ position 0.0；
2. 两个转换函数互为反函数（全程往返）；
3. 三档快捷按钮：高画质→滑杆 30（左半区）/position 0.7；均衡→60/0.4；最小体积→95（右半区）/0.05；
4. 前沿图点击：点最小体积点→滑杆跳到右端 100；点最高画质点→滑杆跳到左端 0；
5. 滑杆两端对应的聚合前沿点信息（体积/清晰度）方向正确；
6. 程序默认打开即均衡（滑杆 60 / position 0.4）。
"""
import os
import sys

# --- 统一路径引导（见 tests/_bootstrap.py，跨子目录复用）---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _bootstrap import ROOT, PROGRAMS  # noqa: E402

import tkinter as tk

from main import App

MB = 1024 * 1024


class StubCanvas:
    """记录高亮调用的画布桩，无需真正渲染窗口。"""
    def __init__(self):
        self.highlighted = None

    def winfo_exists(self):
        return True

    def delete(self, *a):
        pass

    def create_oval(self, *a, **k):
        pass

    def create_text(self, *a, **k):
        pass


def make_app():
    root = tk.Tk()
    root.withdraw()
    return root, App(root)


def main():
    failures = []

    def check(name, cond):
        print(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    root, app = make_app()

    print("[1] 滑杆两端与标签一一对应")
    app.slider_var.set(0.0)
    check("左端 0「画质优先」→ position 1.0", app._current_profile().position == 1.0)
    app.slider_var.set(100.0)
    check("右端 100「体积优先」→ position 0.0", app._current_profile().position == 0.0)

    print("[2] 转换函数互为反函数")
    ok_rt = True
    for v in (0, 10, 25, 40, 50, 60, 75, 90, 100):
        app.slider_var.set(float(v))
        pos = app._slider_to_position()
        back = app._position_to_slider(pos)
        if abs(back - v) > 1e-9:
            ok_rt = False
    check("滑杆值→position→滑杆值 全程往返不变", ok_rt)

    print("[3] 三档快捷按钮")
    app._set_slider("high")
    check("高画质 → position 0.7", abs(app._current_profile().position - 0.7) < 1e-9)
    check("高画质 → 滑杆 30（左半=画质优先侧）", abs(app.slider_var.get() - 30.0) < 1e-9)
    app._set_slider("balanced")
    check("均衡 → position 0.4", abs(app._current_profile().position - 0.4) < 1e-9)
    check("均衡 → 滑杆 60", abs(app.slider_var.get() - 60.0) < 1e-9)
    app._set_slider("smallest")
    check("最小体积 → position 0.05", abs(app._current_profile().position - 0.05) < 1e-9)
    check("最小体积 → 滑杆 95（右半=体积优先侧）", abs(app.slider_var.get() - 95.0) < 1e-9)

    print("[4] 前沿图点击联动（20 个点，按体积升序）")
    points = [{"size": (1 + i) * MB, "psnr": round(28.0 + i * 1.8, 1)} for i in range(20)]
    app.pareto_points = points
    app.pareto_canvas = StubCanvas()
    app._pareto_sx = lambda s: s / MB
    app._pareto_sy = lambda p: p
    app._on_chart_point_click(0)
    check("点最小体积点(idx0) → 滑杆跳到右端 100", abs(app.slider_var.get() - 100.0) < 1e-9)
    app._on_chart_point_click(19)
    check("点最高画质点(idx19) → 滑杆跳到左端 0", abs(app.slider_var.get() - 0.0) < 1e-9)

    print("[5] 聚合前沿点信息方向")
    app.slider_var.set(0.0)
    info_left = app._current_point_info(app._slider_to_position())
    app.slider_var.set(100.0)
    info_right = app._current_point_info(app._slider_to_position())
    check("画质优先端显示最大体积点(20MB)", info_left[0] == "20 MB")
    check("体积优先端显示最小体积点(1MB)", info_right[0] == "1 MB")

    print("[6] 默认状态")
    root2, app2 = make_app()
    check("默认滑杆 60", abs(app2.slider_var.get() - 60.0) < 1e-9)
    check("默认 position 0.4（均衡）", abs(app2._current_profile().position - 0.4) < 1e-9)
    root2.destroy()

    root.destroy()
    print("=" * 68)
    if failures:
        print("映射测试存在 %d 项失败：%s" % (len(failures), failures))
        sys.exit(1)
    print("映射测试全部通过 ✓（滑杆标签与 position 严格一一对应）")


if __name__ == "__main__":
    main()
