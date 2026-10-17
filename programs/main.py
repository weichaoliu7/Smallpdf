# -*- coding: utf-8 -*-
"""
PDF 压缩器 - 桌面图形界面程序。

纯本机运行，不启动任何服务器、不联网，文件不会离开当前电脑。
界面基于 Python 自带的 tkinter，打包后为单个 exe。
"""
from __future__ import annotations

import math
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:  # 运行环境缺少 tkinterdnd2 时退化为普通窗口
    _HAS_DND = False

from app.compressor import (
    PRESETS,
    analyze_pdf_frontier,
    build_profile,
    compress_pdf_bounded,
    enable_parallel_support,
    fmt_mb,
    human_size,
)

APP_NAME = "PDF 压缩器"
# 输出文件名后缀（保持英文，避免不同系统下的编码问题）
OUT_SUFFIX = "_compressed"


def enable_dpi_awareness() -> None:
    """开启高 DPI 适配，让界面在高分屏（Windows 10 及以上）上保持清晰。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        # 后台压缩线程与界面线程之间传递消息的队列
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        # 取消标志：后台线程每处理完一个文件会检查一次
        self.cancel_event = threading.Event()
        self.running = False
        # 源文件路径 -> 压缩后文件路径（双击列表行时打开）
        self.outputs: dict[str, str] = {}
        # 超出目标体积的文件报告（用于结束后统一提示）
        self.limit_reports: list = []
        # 帕累托前沿点（压缩过程中收集，用于绘制前沿图）
        self.frontier_points: list = []

        # 界面状态变量
        # 滑杆 0=左端（标签「画质优先」，对应 position 1），100=右端（标签「体积优先」，
        # 对应 position 0）；默认均衡 position 0.4，即滑杆 60。
        self.slider_var = tk.DoubleVar(value=60.0)        # 滑杆 0~100，经 _slider_to_position 映射为前沿位置
        self.output_mode = tk.IntVar(value=0)             # 0=源文件同目录，1=自定义目录
        self.output_dir_var = tk.StringVar()
        self.strip_meta_var = tk.BooleanVar(value=False)  # 是否清除元数据
        self.target_size_var = tk.StringVar()             # 目标体积上限（MB，可选）
        self.status_var = tk.StringVar(
            value="请添加 PDF 文件（可直接拖入窗口），用滑杆选择清晰度/体积平衡后点击「开始压缩」。")

        self._build_ui()
        self._bind_events()
        self._register_dnd()
        self.root.after(100, self._poll_queue)

        # 支持把 PDF 文件直接拖到 exe 图标上打开（路径作为命令行参数传入）
        for arg in sys.argv[1:]:
            if os.path.isfile(arg) and arg.lower().endswith(".pdf"):
                self._add_files([arg])

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("780x600")
        self.root.minsize(680, 540)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        default_font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", default_font)
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", font=("Microsoft YaHei UI Semibold", 11))

        pad = {"padx": 10, "pady": 6}

        # ---- 文件列表区域 ----
        files_frame = ttk.LabelFrame(self.root, text=" PDF 文件（可将 PDF 直接拖入本窗口） ")
        files_frame.pack(fill="both", expand=True, **pad)

        btn_row = ttk.Frame(files_frame)
        btn_row.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(btn_row, text="添加文件",
                   command=self._on_add).pack(side="left")
        ttk.Button(btn_row, text="移除选中",
                   command=self._on_remove).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="清空列表",
                   command=self._on_clear).pack(side="left", padx=(8, 0))

        tree_frame = ttk.Frame(files_frame)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.tree = ttk.Treeview(tree_frame,
                                 columns=("file", "size", "status"),
                                 show="headings", selectmode="extended")
        self.tree.heading("file", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("file", width=340, anchor="w")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=260, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ---- 压缩方式区域 ----
        level_frame = ttk.LabelFrame(self.root, text=" 压缩方式 ")
        level_frame.pack(fill="x", **pad)

        slider_row = ttk.Frame(level_frame)
        slider_row.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(slider_row, text="画质优先").pack(side="left")
        self.slider = ttk.Scale(slider_row, from_=0, to=100,
                                variable=self.slider_var,
                                command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(slider_row, text="体积优先").pack(side="left")

        preset_row = ttk.Frame(level_frame)
        preset_row.pack(fill="x", padx=12, pady=(4, 0))
        ttk.Button(preset_row, text="高画质",
                   command=lambda: self._set_slider("high")).pack(side="left", padx=(0, 8))
        ttk.Button(preset_row, text="均衡",
                   command=lambda: self._set_slider("balanced")).pack(side="left", padx=(0, 8))
        ttk.Button(preset_row, text="最小体积",
                   command=lambda: self._set_slider("smallest")).pack(side="left")

        self.level_desc = ttk.Label(level_frame, text="", wraplength="730",
                                    foreground="#444444", justify="left")
        self.level_desc.pack(anchor="w", padx=12, pady=(4, 8))
        self._update_level_desc()

        # ---- 输出位置区域 ----
        out_frame = ttk.LabelFrame(self.root, text=" 输出位置 ")
        out_frame.pack(fill="x", **pad)
        ttk.Radiobutton(out_frame,
                        text="保存到源文件所在文件夹（文件名加后缀 %s）" % OUT_SUFFIX,
                        value=0, variable=self.output_mode,
                        command=self._update_output_state
                        ).pack(anchor="w", padx=12, pady=(8, 0))
        custom_row = ttk.Frame(out_frame)
        custom_row.pack(anchor="w", padx=12, pady=(4, 8), fill="x")
        self.custom_radio = ttk.Radiobutton(
            custom_row, text="保存到自定义文件夹：", value=1,
            variable=self.output_mode, command=self._update_output_state)
        self.custom_radio.pack(side="left")
        self.out_entry = ttk.Entry(custom_row, textvariable=self.output_dir_var)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_btn = ttk.Button(custom_row, text="浏览...",
                                     command=self._on_browse_output)
        self.browse_btn.pack(side="left")
        self._update_output_state()

        # ---- 可选项 ----
        opt_frame = ttk.Frame(self.root)
        opt_frame.pack(fill="x", **pad)
        ttk.Checkbutton(opt_frame,
                        text="清除文档元数据（作者、标题等，不影响画面质量）",
                        variable=self.strip_meta_var).pack(anchor="w")

        target_row = ttk.Frame(opt_frame)
        target_row.pack(anchor="w", pady=(4, 0), fill="x")
        ttk.Label(target_row, text="目标体积上限：").pack(side="left")
        self.target_entry = ttk.Entry(target_row, textvariable=self.target_size_var,
                                      width=10)
        self.target_entry.pack(side="left")
        ttk.Label(target_row, text=" MB（留空表示不限制；超限时自动逐级增强压缩）"
                  ).pack(side="left", padx=(4, 0))

        # ---- 底部：进度条 + 操作按钮 ----
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate",
                                        maximum=100)
        self.progress.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var,
                  wraplength=750, justify="left").pack(anchor="w",
                                                       pady=(4, 0))
        action_row = ttk.Frame(bottom)
        action_row.pack(fill="x", pady=(6, 0))
        self.compress_btn = ttk.Button(action_row, text="开始压缩",
                                       style="Accent.TButton",
                                       command=self._on_compress)
        self.compress_btn.pack(side="right")
        self.cancel_btn = ttk.Button(action_row, text="取消",
                                     command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 8))

    def _bind_events(self) -> None:
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 拖拽支持
    def _register_dnd(self) -> None:
        """注册窗口的拖入文件支持（依赖 tkinterdnd2）。"""
        if not _HAS_DND:
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        """处理拖入的文件，仅接受 .pdf 文件。"""
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            return
        pdfs = [p for p in paths if os.path.isfile(p) and p.lower().endswith(".pdf")]
        if pdfs:
            self._add_files(pdfs)

    # ------------------------------------------------------------ 用户操作
    def _on_add(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if paths:
            self._add_files(list(paths))

    def _add_files(self, paths: list[str]) -> None:
        existing = set(self.tree.get_children())
        added = False
        for path in paths:
            path = os.path.normpath(path)
            if path in existing:
                continue
            try:
                size = human_size(os.path.getsize(path))
            except OSError:
                size = ""
            self.tree.insert("", "end", iid=path,
                             values=(os.path.basename(path), size, ""))
            added = True
        # 拖入/上传 PDF 后立即在后台分析并自动生成帕累托前沿图
        if added and paths:
            self._analyze_pareto(paths[-1])

    def _analyze_pareto(self, path: str) -> None:
        """后台分析 PDF，计算帕累托前沿并弹出前沿图。"""
        self.status_var.set("正在分析「%s」并生成帕累托前沿图..." % os.path.basename(path))
        thread = threading.Thread(target=self._pareto_worker,
                                  args=(path,), daemon=True)
        thread.start()

    def _pareto_worker(self, path: str) -> None:
        points, src_size = analyze_pdf_frontier(path)
        self.ui_queue.put(("pareto_chart", points, src_size))

    def _on_remove(self) -> None:
        for item in self.tree.selection():
            self.tree.delete(item)
            self.outputs.pop(item, None)

    def _on_clear(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.outputs.clear()

    def _on_browse_output(self) -> None:
        folder = filedialog.askdirectory(title="选择输出文件夹")
        if folder:
            self.output_mode.set(1)
            self.output_dir_var.set(folder)
            self._update_output_state()

    # ----------------------------------------------------- 滑杆 ↔ 前沿位置
    def _slider_to_position(self) -> float:
        """滑杆值 -> 帕累托前沿位置 position。

        与滑杆标签严格一一对应：
        - 滑杆左端 0（「画质优先」）→ position 1.0（最高画质/源文件端）；
        - 滑杆右端 100（「体积优先」）→ position 0.0（最小体积端）。
        """
        return 1.0 - self.slider_var.get() / 100.0

    def _position_to_slider(self, position: float) -> float:
        """帕累托前沿位置 position -> 滑杆值（_slider_to_position 的反函数）。"""
        return (1.0 - max(0.0, min(1.0, position))) * 100.0

    def _update_level_desc(self) -> None:
        pos = self._slider_to_position()
        label = self._nearest_preset_label(pos)
        info = self._current_point_info(pos)
        if info:
            text = ("画质-体积轴：位置 %.0f%%（最接近「%s」）→ "
                    "体积 %s，清晰度 %.1f dB。" % (pos * 100, label, info[0], info[1]))
        else:
            text = ("画质-体积轴：位置 %.0f%%（最接近「%s」）。"
                    "添加 PDF 后将显示该点的体积与清晰度。" % (pos * 100, label))
        self.level_desc.configure(text=text)

    def _current_point_info(self, pos: float):
        """返回当前滑杆位置对应的聚合前沿点的 (体积字符串, 清晰度 dB)。"""
        pts = getattr(self, "pareto_points", None)
        if not pts:
            return None
        n = max(1, len(pts) - 1)
        idx = int(round(max(0.0, min(1.0, pos)) * n))
        p = pts[idx]
        return (fmt_mb(p["size"]), p["psnr"])

    def _nearest_preset_label(self, pos: float) -> str:
        best_key, best_diff = "balanced", None
        for key in ("high", "balanced", "smallest"):
            diff = abs(pos - PRESETS[key].position)
            if best_diff is None or diff < best_diff:
                best_diff, best_key = diff, key
        return PRESETS[best_key].label

    def _on_slider(self, _event=None) -> None:
        self._update_level_desc()
        # 滑杆拖动时，前沿图上同步高亮对应点
        self._sync_chart_highlight(self._slider_to_position())

    def _set_slider(self, key: str) -> None:
        """把滑杆跳到三档快捷位（高画质 / 均衡 / 最小体积，均为帕累托前沿上的点）。"""
        self.slider_var.set(self._position_to_slider(PRESETS[key].position))
        self._update_level_desc()
        self._sync_chart_highlight(PRESETS[key].position)

    def _current_profile(self):
        """根据界面状态构建压缩配置（画质-体积轴位置即前沿位置）。"""
        p = build_profile("balanced", strip_metadata=self.strip_meta_var.get())
        p.position = self._slider_to_position()
        return p

    def _update_output_state(self) -> None:
        custom = self.output_mode.get() == 1
        self.out_entry.configure(state="normal" if custom else "disabled")
        self.browse_btn.configure(state="normal" if custom else "disabled")

    def _on_tree_double_click(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        target = self.outputs.get(path)
        if target and os.path.exists(target):
            try:
                os.startfile(target)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel(
                    "退出", "压缩仍在进行中，确定要退出吗？"):
                return
            self.cancel_event.set()
        self.root.destroy()

    def _on_cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在取消，将在当前文件处理完成后停止...")

    def _on_compress(self) -> None:
        files = list(self.tree.get_children())
        if not files:
            messagebox.showinfo(APP_NAME, "请先添加至少一个 PDF 文件。")
            return
        if self.output_mode.get() == 1:
            out_dir = self.output_dir_var.get().strip()
            if not out_dir or not os.path.isdir(out_dir):
                messagebox.showwarning(
                    APP_NAME, "请选择有效的自定义输出文件夹。")
                return

        # 重置上一次的状态显示
        for path in files:
            cur = self.tree.item(path, "values")
            self.tree.item(path, values=(cur[0], cur[1], ""))
        self.outputs.clear()
        self.limit_reports.clear()
        self.frontier_points.clear()
        self.progress.configure(value=0)
        self.cancel_event.clear()
        self.running = True
        self.compress_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        # 压缩在后台线程执行，避免界面卡死
        thread = threading.Thread(target=self._worker,
                                  args=(files,), daemon=True)
        thread.start()

    # ------------------------------------------------------------- 后台线程
    def _derive_output(self, src: str) -> str:
        """根据输出设置生成不重名的目标文件路径。"""
        if self.output_mode.get() == 1:
            folder = self.output_dir_var.get().strip()
        else:
            folder = os.path.dirname(src)
        name = os.path.basename(src)
        stem, ext = os.path.splitext(name)
        candidate = os.path.join(folder, stem + OUT_SUFFIX + ext)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(
                folder, "%s%s_%d%s" % (stem, OUT_SUFFIX, counter, ext))
            counter += 1
        return candidate

    def _worker(self, files: list[str]) -> None:
        profile = self._current_profile()
        target_bytes = self._parse_target_bytes()
        total_n = len(files)
        done = failed = 0

        for index, src in enumerate(files):
            if self.cancel_event.is_set():
                self.ui_queue.put(("file_status", src, "已取消"))
                continue
            name = os.path.basename(src)
            self.ui_queue.put((
                "status",
                "正在压缩 %s（%d/%d）..." % (name, index + 1, total_n)))
            self.ui_queue.put(("file_status", src, "处理中..."))
            try:
                dst = self._derive_output(src)

                def progress(pct: float, msg: str,
                             _idx=index, _n=total_n, _name=name) -> None:
                    # 单文件进度换算为整体进度
                    overall = (_idx + pct / 100.0) / _n * 100
                    self.ui_queue.put(
                        ("progress", overall, "%s - %s" % (_name, msg)))

                report = compress_pdf_bounded(src, dst, profile, target_bytes, progress)
                # 收集帕累托前沿点，供界面绘制前沿图
                if report.get("frontier_points"):
                    self.ui_queue.put(("frontier", report["frontier_points"]))

                if target_bytes is not None and not report["within_limit"]:
                    # 超出目标体积：给出原因与进一步优化建议
                    text = "⚠ 超限：%s → %s（目标 %s，已用最强档位）" % (
                        human_size(report["original_size"]),
                        human_size(report["final_size"]),
                        human_size(target_bytes))
                    self.ui_queue.put(("limit_warn", src, dst, text, report))
                elif report["fallback_kept_original"]:
                    text = "已优化至极限，保留原文件（%s）" % human_size(
                        report["final_size"])
                else:
                    text = "完成：%s → %s（减小 %.1f%%）" % (
                        human_size(report["original_size"]),
                        human_size(report["final_size"]),
                        report["saved_percent"])
                self.ui_queue.put(("file_done", src, dst, text))
                done += 1
            except Exception as exc:
                text = "失败：%s" % exc
                self.ui_queue.put(("file_status", src, text))
                failed += 1

        if self.cancel_event.is_set():
            summary = "已取消：完成 %d 个，失败 %d 个。" % (done, failed)
        else:
            summary = "全部完成：成功 %d 个，失败 %d 个。" % (done, failed)
        self.ui_queue.put(("all_done", summary))

    def _parse_target_bytes(self) -> "Optional[int]":
        """把“目标体积上限（MB）”输入解析为字节数；无效或留空返回 None。"""
        raw = self.target_size_var.get().strip()
        if not raw:
            return None
        try:
            mb = float(raw)
        except ValueError:
            return None
        if mb <= 0:
            return None
        return int(mb * 1024 * 1024)

    # ----------------------------------------------------- 界面消息轮询
    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, pct, msg = event
                    self.progress.configure(value=pct)
                    self.status_var.set(msg)
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "file_status":
                    _, path, text = event
                    self._set_row_status(path, text)
                elif kind == "file_done":
                    _, path, dst, text = event
                    self.outputs[path] = dst
                    self._set_row_status(path, text)
                elif kind == "limit_warn":
                    _, path, dst, text, report = event
                    self.outputs[path] = dst
                    self._set_row_status(path, text)
                    self.limit_reports.append((path, report))
                elif kind == "frontier":
                    self.frontier_points.extend(event[1])
                elif kind == "pareto_chart":
                    self._show_pareto_chart(event[1], event[2])
                    self.status_var.set("帕累托前沿图已生成，可点击「开始压缩」。")
                elif kind == "all_done":
                    self._on_worker_finished(event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_row_status(self, path: str, text: str) -> None:
        if not self.tree.exists(path):
            return
        values = self.tree.item(path, "values")
        self.tree.item(path, values=(values[0], values[1], text))

    def _on_worker_finished(self, summary: str) -> None:
        self.running = False
        self.progress.configure(value=100)
        self.status_var.set(
            summary + " 双击列表中的文件可打开压缩结果。")
        self.compress_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

        # 有文件超出目标体积时，统一弹出说明（原因 + 进一步优化建议）
        if self.limit_reports:
            self._show_limit_summary()

    # -------------------------------------------------------------- 帕累托前沿图
    _P_LEFT, _P_TOP = 72, 18

    def _pareto_layout(self):
        """返回当前画布的绘图区（左、上、右、下，像素），随窗口尺寸自适应。"""
        w = self.pareto_canvas.winfo_width()
        h = self.pareto_canvas.winfo_height()
        right = max(self._P_LEFT + 10, w - 16)
        bottom = max(self._P_TOP + 10, h - 54)
        return self._P_LEFT, self._P_TOP, right, bottom

    def _pareto_ticks(self, xmin, xmax):
        """在字节区间 [xmin, xmax]（线性）内生成 nice 刻度值（MB 取整，返回字节）。"""
        import math as _m
        MB = 1024 * 1024
        span_mb = (xmax - xmin) / MB
        if span_mb <= 0:
            return [int(xmin)]
        raw_step_mb = span_mb / 8.0
        mag = 10 ** int(_m.floor(_m.log10(max(1e-9, raw_step_mb))))
        step_mb = raw_step_mb
        for m in (1, 2, 5, 10):
            if m * mag >= raw_step_mb:
                step_mb = m * mag
                break
        step = step_mb * MB
        ticks = []
        v = int(_m.ceil(xmin / step)) * step
        while v <= xmax:
            ticks.append(v)
            v += step
        return ticks

    def _show_pareto_chart(self, points: list, source_size: int = 0) -> None:
        """弹出帕累托前沿图窗口（与「画质-体积轴」双向联动，支持缩放/平移）。"""
        if not points:
            return
        self.pareto_points = points
        self.pareto_source_size = source_size

        if getattr(self, "pareto_win", None) and self.pareto_win.winfo_exists():
            win = self.pareto_win
            canvas = self.pareto_canvas
        else:
            win = tk.Toplevel(self.root)
            win.title("帕累托前沿图（体积 ↔ 清晰度）")
            win.geometry("860x620")
            win.transient(self.root)
            bar = ttk.Frame(win)
            bar.pack(fill="x", padx=10, pady=(10, 0))
            ttk.Button(bar, text="重置视图", command=self._reset_pareto_view).pack(side="left")
            ttk.Label(bar, text="滚轮缩放 · 拖动平移 · 点击散点选点",
                      foreground="#666666").pack(side="left", padx=10)
            canvas = tk.Canvas(win, background="#ffffff", highlightthickness=0)
            canvas.pack(fill="both", expand=True, padx=10, pady=10)
            self.pareto_win = win
            self.pareto_canvas = canvas
            canvas.bind("<MouseWheel>", self._on_pareto_zoom)
            canvas.bind("<Button-4>", self._on_pareto_zoom)
            canvas.bind("<Button-5>", self._on_pareto_zoom)
            canvas.bind("<ButtonPress-1>", self._on_pareto_press)
            canvas.bind("<B1-Motion>", self._on_pareto_drag)
            canvas.bind("<ButtonRelease-1>", self._on_pareto_release)
            canvas.bind("<Configure>", self._on_pareto_configure)

        self._reset_pareto_view()

    def _reset_pareto_view(self) -> None:
        """根据数据范围重置视口（数据空间：线性体积 ↔ PSNR 20~100）。"""
        sizes = [p["size"] for p in self.pareto_points]
        if self.pareto_source_size > 0:
            sizes.append(self.pareto_source_size)
        s_min = min(sizes)
        s_max = max(sizes)
        if s_max <= s_min:
            s_max = s_min + 1
        pad = max(1.0, (s_max - s_min) * 0.06)
        self._pareto_view = [s_min - pad, s_max + pad, 20.0, 100.0]
        self._redraw_pareto()

    def _on_pareto_configure(self, _event) -> None:
        """窗口/画布尺寸变化时防抖重绘，避免放大窗口后出现空白。"""
        job = getattr(self, "_pareto_redraw_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._pareto_redraw_job = self.root.after(60, self._redraw_pareto)

    def _redraw_pareto(self) -> None:
        """根据当前视口重绘整张帕累托前沿图（含横竖分割线）。"""
        canvas = self.pareto_canvas
        import math as _m
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 50 or h < 50:
            self.root.after(50, self._redraw_pareto)
            return
        canvas.delete("all")
        left, top, right, bottom = self._pareto_layout()
        xmin, xmax, ymin, ymax = self._pareto_view

        def sx(s):
            return left + (right - left) * (s - xmin) / max(1e-9, xmax - xmin)

        def sy(p):
            return bottom - (bottom - top) * (p - ymin) / max(1e-9, ymax - ymin)

        self._pareto_sx = sx
        self._pareto_sy = sy

        # ---- 网格线（横竖分割线）+ 刻度 ----
        for t in self._pareto_ticks(xmin, xmax):
            x = sx(t)
            canvas.create_line(x, top, x, bottom, fill="#e8e8e8")      # 竖分割线
            canvas.create_line(x, bottom, x, bottom + 5, fill="#555")  # 刻度
            canvas.create_text(x, bottom + 16, text=fmt_mb(t),
                               fill="#444", font=("Microsoft YaHei UI", 8))
        ystep = 10 if (ymax - ymin) > 60 else (5 if (ymax - ymin) > 30 else 2)
        for p in range(int(_m.ceil(ymin / ystep)) * ystep, int(ymax) + 1, ystep):
            y = sy(p)
            canvas.create_line(left, y, right, y, fill="#e8e8e8")      # 横分割线
            canvas.create_line(left, y, left - 5, y, fill="#555")
            canvas.create_text(left - 8, y, text=str(p), anchor="e",
                               fill="#444", font=("Microsoft YaHei UI", 8))

        # ---- 坐标轴 ----
        canvas.create_line(left, bottom, right, bottom, fill="#555")
        canvas.create_line(left, top, left, bottom, fill="#555")
        canvas.create_text((left + right) / 2, h - 16,
                           text="体积（MB）", fill="#333",
                           font=("Microsoft YaHei UI", 9))
        canvas.create_text(14, (top + bottom) / 2, text="清晰度 PSNR（dB）",
                           angle=90, fill="#333", font=("Microsoft YaHei UI", 9))

        # ---- 清晰可读下限参考线 ----
        try:
            from app.compressor import _MIN_READABLE_PSNR
        except Exception:
            _MIN_READABLE_PSNR = 28.0
        if ymin <= _MIN_READABLE_PSNR <= ymax:
            y_floor = sy(_MIN_READABLE_PSNR)
            canvas.create_line(left, y_floor, right, y_floor, dash=(4, 4), fill="#e07a5f")
            canvas.create_text(right - 4, y_floor - 8,
                               text="清晰可读下限 %d dB" % _MIN_READABLE_PSNR,
                               anchor="e", fill="#c05b3c", font=("Microsoft YaHei UI", 9))

        # ---- 前沿散点（可点击选点） ----
        for i, p in enumerate(self.pareto_points):
            x, y = sx(p["size"]), sy(p["psnr"])
            if not (left - 6 <= x <= right + 6 and top - 6 <= y <= bottom + 6):
                continue
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                               fill="#3d7bd6", outline="", tags="fp_%d" % i)

        # ---- 源文件点（红色方块） ----
        if self.pareto_source_size > 0:
            xs_ = sx(self.pareto_source_size)
            ys_ = sy(99.0)
            if left - 8 <= xs_ <= right + 8 and top <= ys_ <= bottom:
                canvas.create_rectangle(xs_ - 6, ys_ - 6, xs_ + 6, ys_ + 6,
                                        fill="#d64545", outline="#a32d2d", width=2)
                canvas.create_text(xs_, ys_ + 18,
                                   text="源文件\n%s" % fmt_mb(self.pareto_source_size),
                                   fill="#a32d2d", justify="center",
                                   font=("Microsoft YaHei UI", 9))

        self._sync_chart_highlight(self._slider_to_position())

    # ---- 缩放 / 平移 ----
    def _pareto_pixel_to_data(self, px, py):
        """画布像素坐标 -> 数据坐标 (线性体积, PSNR)。"""
        left, top, right, bottom = self._pareto_layout()
        xmin, xmax, ymin, ymax = self._pareto_view
        lv = xmin + (px - left) / max(1, right - left) * (xmax - xmin)
        pv = ymin + (bottom - py) / max(1, bottom - top) * (ymax - ymin)
        return lv, pv

    def _on_pareto_zoom(self, event) -> None:
        """滚轮缩放：以光标为锚点放大/缩小视口。"""
        num = getattr(event, "num", None)
        if num == 4:
            factor = 0.9
        elif num == 5:
            factor = 1.1
        else:
            delta = getattr(event, "delta", 0)
            if delta == 0:
                return
            factor = 0.9 if delta > 0 else 1.1
        dx, dy = self._pareto_pixel_to_data(event.x, event.y)
        xmin, xmax, ymin, ymax = self._pareto_view
        self._pareto_view = [
            dx - (dx - xmin) * factor, dx + (xmax - dx) * factor,
            dy - (dy - ymin) * factor, dy + (ymax - dy) * factor,
        ]
        self._redraw_pareto()

    def _on_pareto_press(self, event) -> None:
        self._pareto_drag_origin = (event.x, event.y)
        self._pareto_view_origin = tuple(self._pareto_view)
        self._pareto_dragged = False

    def _on_pareto_drag(self, event) -> None:
        origin = getattr(self, "_pareto_drag_origin", None)
        if origin is None:
            return
        dx = event.x - origin[0]
        dy = event.y - origin[1]
        if abs(dx) + abs(dy) < 4:
            return
        self._pareto_dragged = True
        left, top, right, bottom = self._pareto_layout()
        xmin, xmax, ymin, ymax = self._pareto_view_origin
        dx_data = -(dx) / max(1, right - left) * (xmax - xmin)
        dy_data = (dy) / max(1, bottom - top) * (ymax - ymin)
        self._pareto_view = [xmin + dx_data, xmax + dx_data,
                             ymin + dy_data, ymax + dy_data]
        self._redraw_pareto()

    def _on_pareto_release(self, event) -> None:
        if getattr(self, "_pareto_dragged", False):
            self._pareto_dragged = False
            return
        # 未拖动 = 点击：选中最近的散点
        best_idx, best_dist = None, None
        for i, p in enumerate(self.pareto_points):
            x = self._pareto_sx(p["size"])
            y = self._pareto_sy(p["psnr"])
            d = (x - event.x) ** 2 + (y - event.y) ** 2
            if best_dist is None or d < best_dist:
                best_dist, best_idx = d, i
        if best_idx is not None and best_dist <= 12 ** 2:
            self._on_chart_point_click(best_idx)

    def _on_chart_point_click(self, idx: int) -> None:
        """点击前沿图上的点 → 显示该点体积/清晰度，并联动「画质-体积轴」。"""
        n = max(1, len(self.pareto_points) - 1)
        # 图表点按体积升序（idx 0=最小体积），pos 即前沿位置 position
        pos = idx / n
        p = self.pareto_points[idx]
        self.slider_var.set(self._position_to_slider(pos))
        self._update_level_desc()
        self._sync_chart_highlight(pos)
        canvas = self.pareto_canvas
        x, y = self._pareto_sx(p["size"]), self._pareto_sy(p["psnr"])
        canvas.delete("ptinfo")
        canvas.create_text(x + 12, y - 12, anchor="w", tags="ptinfo",
                           text="体积 %s\n清晰度 %.1f dB" % (
                               fmt_mb(p["size"]), p["psnr"]),
                           fill="#1a5fb4", font=("Microsoft YaHei UI", 9))

    def _sync_chart_highlight(self, position: float) -> None:
        """滑杆位置 → 在前沿图上高亮对应点。"""
        canvas = getattr(self, "pareto_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        if not getattr(self, "pareto_points", None):
            return
        sx = getattr(self, "_pareto_sx", None)
        sy = getattr(self, "_pareto_sy", None)
        if sx is None or sy is None:
            return
        n = max(1, len(self.pareto_points) - 1)
        idx = int(round(max(0.0, min(1.0, position)) * n))
        p = self.pareto_points[idx]
        x, y = sx(p["size"]), sy(p["psnr"])
        canvas.delete("hl")
        canvas.create_oval(x - 8, y - 8, x + 8, y + 8,
                           outline="#e07a5f", width=3, tags="hl")

    def _show_limit_summary(self) -> None:
        """把超出目标体积的文件的原因与建议汇总提示给用户。"""
        lines = []
        for path, report in self.limit_reports:
            lines.append("文件：%s" % os.path.basename(path))
            lines.append("  结果：%s（目标 %s）" % (
                human_size(report["final_size"]),
                human_size(report.get("target_bytes") or 0)))
            if report.get("limit_reason"):
                lines.append("  原因：%s" % report["limit_reason"])
            for s in report.get("suggestions", []):
                lines.append("  建议：%s" % s)
            lines.append("")
        messagebox.showwarning(
            APP_NAME,
            "以下文件未能满足目标体积上限：\n\n" + "\n".join(lines))
        self.limit_reports.clear()


def _selftest(argv: list[str]) -> int:
    """无界面自检：``PDFCompressor.exe --selftest <输入.pdf> <输出.pdf> [position]``

    用于验证打包后的 exe 在真实环境下（尤其是多进程并行路径）与源码运行
    完全一致。结果写入 ``<输出>.report.txt`` —— 写成文件而非打印，是因为
    正式版以 --windowed 构建，没有控制台。双击正常启动时不会进入此分支。
    """
    import time

    from app.compressor import (
        _auto_workers,
        _MP_DISABLED,
        analyze_pdf_frontier,
        build_profile,
        compress_pdf_bounded,
        get_plans,
        verify_compression,
    )

    if len(argv) < 2:
        return 2
    src, dst = argv[0], argv[1]
    pos = float(argv[2]) if len(argv) > 2 else 0.4
    lines = ["自检报告", "输入: %s" % src, "输出: %s" % dst,
             "position: %.2f" % pos,
             "CPU 核数: %s" % os.cpu_count(),
             "并行工作进程数: %d" % _auto_workers(), ""]

    try:
        t = time.perf_counter()
        plans = get_plans(src)
        t_plan = time.perf_counter() - t
        lines.append("1) 位图规划: %d 张，耗时 %.2f s" % (len(plans), t_plan))

        t = time.perf_counter()
        pts, src_size = analyze_pdf_frontier(src)
        t_front = time.perf_counter() - t
        lines.append("2) 聚合前沿: %d 点，耗时 %.2f s" % (len(pts), t_front))
        if pts:
            lines.append("   体积区间 %.2f ~ %.2f MB，清晰度 %.1f ~ %.1f dB"
                         % (pts[0]["size"] / 1048576, pts[-1]["size"] / 1048576,
                            pts[0]["psnr"], pts[-1]["psnr"]))

        prof = build_profile("balanced")
        prof.position = pos
        t = time.perf_counter()
        rep = compress_pdf_bounded(src, dst, prof, None)
        t_comp = time.perf_counter() - t
        lines.append("3) 真实压缩: 耗时 %.2f s，%s -> %s（节省 %.2f%%）"
                     % (t_comp, human_size(rep["original_size"]),
                        human_size(rep["final_size"]), rep["saved_percent"]))
        lines.append("   位图 %d 张，有损重编码 %d，降采样 %d，转灰度 %d"
                     % (rep["image_count"], rep["lossy_recompressed"],
                        rep["downsampled"], rep["gray_converted"]))
        lines.append("   最差 PSNR %.1f dB，平均 PSNR %.1f dB"
                     % (rep["psnr"], rep["psnr_avg"]))

        v = verify_compression(src, dst)
        lines.append("4) 输出校验: ok=%s，检查位图 %d 张，失败 %d 张，"
                     "最差 PSNR %.1f dB，最低对比度保持 %.2f"
                     % (v["ok"], v["images_checked"], v["failed_images"],
                        v["min_psnr"], v["min_contrast"]))
        for e in v["errors"][:5]:
            lines.append("   ! %s" % e)
        lines.append("")
        lines.append("结论: %s" % ("通过" if v["ok"] and rep["final_size"] > 0 else "失败"))
    except Exception as exc:
        import traceback
        lines.append("异常: %s: %s" % (type(exc).__name__, exc))
        lines.append(traceback.format_exc())

    if _MP_DISABLED:
        lines.append("注意: 进程池不可用，已降级为线程/串行")

    report_path = dst + ".report.txt"
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        return 3
    return 0


def main() -> None:
    # 无界面自检模式（供验证打包后的 exe）
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest(sys.argv[2:]))
    # 打包成 exe 后启用多进程并行（必须早于任何进程池创建）
    enable_parallel_support()
    enable_dpi_awareness()
    if _HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
