# -*- coding: utf-8 -*-
"""
PDF 压缩器 - 核心压缩引擎。

依赖：pikepdf（MPL-2.0，内置 QPDF 为 Apache-2.0）、Pillow（HPND）。
不含 AGPL/GPL 组件，可闭源商业分发；全程离线运行，不访问网络。

画质优先原则：
 1. 矢量内容（文字、线条、矢量图形）绝不做任何有损处理。
 2. 位图是否降采样、降到多少，由「多分辨率 × 编码器 × 质量」候选集的帕累托前沿
    按体积-清晰度目标择优决定（清晰度以约 150 DPI 的阅读分辨率为参考），
    不是预设 DPI 门槛的硬裁剪。
 3. 每张位图都在「保留原数据流 / PNG 风格无损重编码 / JPEG 重编码」之间择优：
    只有当 JPEG 明显小于最优无损方案时才使用 JPEG，否则保留无损方案。
 4. 结构优化（对象流生成、重复位图去重、删除缩略图与未引用资源）完全无损，
    且不对已有 Flate 数据流做二次重压缩（recompress_flate=False，否则会破坏
    PNG 行预测器流导致体积反弹）；若压缩结果反而变大，则原样保留原文件。
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import math
import multiprocessing
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

import pikepdf
from PIL import Image, ImageChops, ImageFilter, ImageStat

# 关闭 Pillow 对超大图片的像素数保护（扫描版 PDF 可能含超大页面位图）
Image.MAX_IMAGE_PIXELS = None


# --------------------------------------------------------------------------------------
# 压缩档位定义
# --------------------------------------------------------------------------------------
@dataclasses.dataclass
class Profile:
    key: str = "balanced"
    label: str = "均衡"
    desc: str = ""
    position: float = 0.4          # 前沿上的位置（0=最小体积，1=最高画质/源文件）
    dedupe: bool = True            # 是否去除完全相同的重复位图
    strip_metadata: bool = False   # 是否清除文档元数据
    drop_thumbnails: bool = True   # 是否删除页面缩略图（不影响正文画面）


PRESETS: Dict[str, Profile] = {
    "high": Profile(
        key="high", label="高画质",
        desc=("帕累托前沿上偏画质的点（位置 0.7）。"
              "文字图表强制 4:4:4、照片 4:2:0，屏幕阅读与打印均清晰。"),
        position=0.7,
        dedupe=True, strip_metadata=False, drop_thumbnails=True,
    ),
    "balanced": Profile(
        key="balanced", label="均衡",
        desc=("帕累托前沿上的均衡点（位置 0.4），清晰度与体积兼顾。"),
        position=0.4,
        dedupe=True, strip_metadata=False, drop_thumbnails=True,
    ),
    "smallest": Profile(
        key="smallest", label="最小体积",
        desc=("帕累托前沿上偏体积的点（位置 0.05），"
              "在保证清晰可读的前提下尽量压缩。"),
        position=0.05,
        dedupe=True, strip_metadata=False, drop_thumbnails=True,
    ),
    # 极限档位：仅在设置了目标体积且常规档位无法满足时，由自动压缩逐级降档
    # 触发，不直接展示给用户，属于“进一步优化”的最后手段。
    "extreme": Profile(
        key="extreme", label="极限压缩",
        desc=("极限压缩：体积优先到极致。"
              "仅用于自动压缩仍无法满足目标体积时作为最后手段。"),
        position=0.0,
        dedupe=True, strip_metadata=False, drop_thumbnails=True,
    ),
}

# 档位顺序（输出从大到小）
PROFILE_ORDER = ("high", "balanced", "smallest")


def build_profile(preset: str = "balanced",
                  strip_metadata: bool = False) -> Profile:
    """根据档位名称生成一份压缩配置，并应用可选项。"""
    base = PRESETS.get(preset, PRESETS["balanced"])
    p = dataclasses.replace(base)
    p.strip_metadata = bool(strip_metadata)
    return p


def human_size(num: float) -> str:
    """把字节数格式化为便于阅读的字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"


def fmt_mb(num: float) -> str:
    """把字节数格式化为以 MB 为单位的字符串（帕累托图/画质轴统一单位）。"""
    mb = num / (1024 * 1024)
    if mb >= 100:
        return "%.0f MB" % mb
    if mb >= 0.01:
        s = "%.2f" % mb
        if s.endswith(".00"):
            s = s[:-3]
        return "%s MB" % s
    return "%.3f MB" % mb


# --------------------------------------------------------------------------------------
# PDF 结构遍历
# --------------------------------------------------------------------------------------
def _mul(m: List[float], n: List[float]) -> List[float]:
    """两个 2x3 仿射变换矩阵相乘（PDF 坐标变换用）。"""
    a0, b0, c0, d0, e0, f0 = m
    a1, b1, c1, d1, e1, f1 = n
    return [
        a0 * a1 + b0 * c1, a0 * b1 + b0 * d1,
        c0 * a1 + d0 * c1, c0 * b1 + d0 * d1,
        e0 * a1 + f0 * c1 + e1, e0 * b1 + f0 * d1 + f1,
    ]


_IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def _placement_map(page) -> Dict[Tuple[int, int], Tuple[float, float]]:
    """解析页面内容流，得到每张位图 XObject 的显示尺寸（单位：点）。

    返回 {(对象号, 生成号): (最大显示宽, 最大显示高)}。
    解析失败时返回空字典，由调用方退回到保守估计，绝不会因此过度降采样。
    """
    result: Dict[Tuple[int, int], Tuple[float, float]] = {}

    def record(obj, ctm) -> None:
        try:
            key = (obj.objgen[0], obj.objgen[1])
        except Exception:
            return
        w = abs(ctm[0]) if abs(ctm[0]) > 1e-6 else abs(ctm[1])
        h = abs(ctm[3]) if abs(ctm[3]) > 1e-6 else abs(ctm[2])
        if w <= 0 or h <= 0:
            return
        prev = result.get(key, (0.0, 0.0))
        result[key] = (max(prev[0], w), max(prev[1], h))

    def walk(instructions, resources, ctm, depth) -> None:
        stack: List[List[float]] = []
        try:
            xobjs = resources.get("/XObject") if resources is not None else None
        except Exception:
            xobjs = None
        for ins in instructions:
            try:
                op = str(ins.operator)
            except Exception:
                continue
            if op == "q":
                stack.append(list(ctm))
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                try:
                    ctm = _mul(ctm, [float(x) for x in ins.operands])
                except Exception:
                    pass
            elif op == "Do":
                if xobjs is None:
                    continue
                try:
                    name = str(ins.operands[0])
                    obj = xobjs.get(name)
                    if obj is None:
                        obj = xobjs.get(pikepdf.Name(name))
                except Exception:
                    continue
                if obj is None:
                    continue
                try:
                    subtype = str(obj.get("/Subtype"))
                except Exception:
                    continue
                if subtype == "/Image":
                    record(obj, ctm)
                elif subtype == "/Form" and depth < 4:
                    # Form XObject 可能嵌套位图，递归进入（限制层数防止死循环）
                    inner = ctm
                    try:
                        matrix = obj.get("/Matrix")
                        if matrix is not None:
                            inner = _mul(ctm, [float(x) for x in matrix])
                    except Exception:
                        pass
                    try:
                        sub_res = obj.get("/Resources")
                        if sub_res is None:
                            sub_res = resources
                        walk(pikepdf.parse_content_stream(obj), sub_res, inner, depth + 1)
                    except Exception:
                        pass

    try:
        res = page.get("/Resources")
        walk(pikepdf.parse_content_stream(page), res, list(_IDENTITY), 0)
    except Exception:
        pass
    return result


def collect_image_refs(pdf: "pikepdf.Pdf") -> Tuple[Dict[Any, List[Tuple[Any, Any]]], Dict[Any, Any]]:
    """收集所有位图对象及其全部引用点。

    同一张位图常被多个页面共享；必须替换所有引用点，否则旧数据流仍被引用，
    文件反而变大。返回 ({对象键: [(容器, 资源名), ...]}, {对象键: 位图对象})。
    """
    refs: Dict[Any, List[Tuple[Any, Any]]] = {}
    samples: Dict[Any, Any] = {}
    for container, name, obj in iter_image_xobjects(pdf):
        try:
            key = obj.objgen if obj.is_indirect else ("id", id(obj))
        except Exception:
            continue
        refs.setdefault(key, []).append((container, name))
        samples.setdefault(key, obj)
    return refs, samples


def iter_image_xobjects(pdf: "pikepdf.Pdf"):
    """遍历文档中全部位图 XObject，包括 Form XObject 内部嵌套的位图。

    间接对象按对象号去重；直接（内联）资源字典统一报告 (0,0)，
    不能按对象号去重，否则会错误漏处理位图。
    """
    visited_forms = set()

    def walk_container(container, depth: int = 0):
        if container is None or depth > 5:
            return
        try:
            xobjs = container.get("/XObject")
        except Exception:
            return
        if xobjs is None:
            return
        for name in list(xobjs.keys()):
            try:
                obj = xobjs[name]
                subtype = str(obj.get("/Subtype"))
            except Exception:
                continue
            if subtype == "/Image":
                yield container, name, obj
            elif subtype == "/Form":
                try:
                    key = obj.objgen if obj.is_indirect else ("id", id(obj))
                except Exception:
                    key = None
                if key is not None:
                    if key in visited_forms:
                        continue
                    visited_forms.add(key)
                yield from walk_container(obj.get("/Resources"), depth + 1)

    for page in pdf.pages:
        try:
            res = page.get("/Resources")
        except Exception:
            continue
        yield from walk_container(res)


# --------------------------------------------------------------------------------------
# 位图编码候选方案
# --------------------------------------------------------------------------------------
def _png_idat(png_bytes: bytes) -> Optional[Tuple[bytes, Dict[str, int]]]:
    """从 PNG 中提取合并后的 IDAT zlib 数据流与像素布局。

    PDF 不能直接嵌入 PNG 容器，但可以用 /FlateDecode 加 /DecodeParms
    <</Predictor 15>> 承载 PNG 的行滤波数据，二者完全等价且无损。
    """
    if len(png_bytes) < 8 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat = 8, bytearray()
    w = h = bit_depth = color_type = 0
    interlace = 0
    try:
        while pos + 8 <= len(png_bytes):
            length = int.from_bytes(png_bytes[pos:pos + 4], "big")
            ctype = png_bytes[pos + 4:pos + 8]
            data = png_bytes[pos + 8:pos + 8 + length]
            if ctype == b"IHDR" and length >= 13:
                w = int.from_bytes(data[0:4], "big")
                h = int.from_bytes(data[4:8], "big")
                bit_depth, color_type = data[8], data[9]
                interlace = data[12]
            elif ctype == b"IDAT":
                idat.extend(data)
            elif ctype == b"IEND":
                break
            pos += 12 + length
    except Exception:
        return None
    if not idat or w <= 0 or h <= 0 or interlace != 0:
        return None
    if bit_depth != 8 or color_type not in (0, 2):
        return None  # 仅支持 8 位灰度 / RGB，其余由上层先转换
    return bytes(idat), {"width": w, "height": h, "colors": 1 if color_type == 0 else 3}


def _encode_png_parts(img: Image.Image) -> Optional[Dict[str, Any]]:
    """构建无损 PDF 位图：主 Flate 数据流（必要时附带 SMask 透明通道）。"""
    try:
        if img.mode in ("RGBA", "LA", "PA"):
            rgba = img.convert("RGBA")
            base = rgba.convert("RGB")
            alpha = rgba.getchannel("A")
        elif img.mode == "P":
            base = img.convert("RGB")
            alpha = None
        elif img.mode == "1":
            base = img.convert("L")
            alpha = None
        elif img.mode == "L":
            base = img
            alpha = None
        else:
            base = img.convert("RGB")
            alpha = None
    except Exception:
        return None

    buf = io.BytesIO()
    try:
        base.save(buf, format="PNG", optimize=True, compress_level=9)
    except Exception:
        return None
    parsed = _png_idat(buf.getvalue())
    if not parsed:
        return None
    idat, layout = parsed

    smask = None
    if alpha is not None:
        abuf = io.BytesIO()
        try:
            alpha.save(abuf, format="PNG", optimize=True, compress_level=9)
            ap = _png_idat(abuf.getvalue())
            if ap:
                smask = {"data": ap[0], "layout": ap[1]}
        except Exception:
            smask = None
        if smask is None:
            return None  # 透明通道无法重建时，绝不写出会丢失透明度的结果

    total = len(idat) + (len(smask["data"]) if smask else 0)
    return {"data": idat, "layout": layout, "smask": smask, "bytes": total}


def _jpeg_bytes(img: Image.Image, quality: int, subsampling: int) -> bytes:
    """按给定质量与色度采样参数把图像编码为 JPEG 字节流。"""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(quality),
             subsampling=int(subsampling), optimize=True, progressive=False)
    return buf.getvalue()


def _jpx_bytes(img: Image.Image, rate: int) -> bytes:
    """按目标压缩比把图像编码为 JPEG 2000（JPX/JP2）字节流。

    JPEG 2000 在同画质下通常比 JPEG 小 20%~50%，是 PDF 1.5+ 支持的高级有损
    编码；在灰度/噪点类扫描件上优势尤为明显。rate 为目标压缩比（如 20 表示
    约 20:1）。仅在调用方确认兼容性可接受时使用。
    """
    if img.mode == "1":
        img = img.convert("L")
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG2000", quality_mode="rates",
             quality_layers=[int(rate)])
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# 多目标优化（速率-失真，Rate-Distortion）
# --------------------------------------------------------------------------------------
# 清晰可读下限：所有帕累托前沿上的点都必须达到该 PSNR，低于此值的候选被剔除，
# 保证任何可选点都「清晰可读」。该阈值由多次测试标定。
_MIN_READABLE_PSNR = 28.0
# 对比度保持下限：文字被洗淡时对比度（灰度标准差）会显著下降；要求候选至少
# 保留源图该比例的对比度，避免「压过头导致文字发灰不可读」。
_MIN_CONTRAST_RATIO = 0.7
# 单图帕累托前沿选点数量上限（候选总数有限，实际非支配点通常 ≤29）。
_FRONTIER_POINTS = 44
# 聚合（全文档）帕累托前沿个体数量：原 20 个 × 2.2 = 44 个。
_AGGREGATE_POINTS = 44
# 清晰度参考分辨率：把源图缩到该最大边长（约 150 DPI 阅读分辨率）再算 PSNR，
# 既符合「清晰可读」的语义，又大幅加速计算。
_READING_DIM = 1500


def _reading_reference(img: Image.Image) -> Image.Image:
    """生成清晰度参考图：源图缩放到阅读分辨率（约 150 DPI）的灰度图。"""
    gray = img.convert("L")
    w, h = gray.size
    if max(w, h) <= _READING_DIM:
        return gray
    s = _READING_DIM / max(w, h)
    return gray.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def _psnr_against(data: bytes, ref_l: Image.Image) -> float:
    """把编码字节流解码回来（必要时上采样到参考尺寸），与参考灰度图比较 PSNR。"""
    return _metrics(data, ref_l, 1.0)[0]


def _hist_moments(img_l: "Image.Image") -> Tuple[int, float, float]:
    """单波段 L 图像的 (像素数, 一阶和, 平方和)。

    与 ``ImageStat.Stat(img).count[0] / sum[0] / sum2[0]`` **逐位等价**：
    Pillow 的 Stat 本身就是对 256 桶直方图做同样累加，此处直接复用直方
    图，省去 Stat 的对象构造与中位数/极值等无用计算。
    """
    h = img_l.histogram()
    n = 0
    s1 = 0.0
    s2 = 0.0
    for i, c in enumerate(h):
        if c:
            n += c
            s1 += i * c
            s2 += (i * i) * c
    return n, s1, s2


def _stddev_from_moments(n: int, s1: float, s2: float) -> float:
    """由直方图矩算标准差，与 ``ImageStat.Stat(img).stddev[0]`` 逐位等价。"""
    if not n:
        return 0.0
    var = (s2 - (s1 ** 2.0) / n) / n
    return math.sqrt(var) if var > 0.0 else 0.0


def _stddev_l(img_l: "Image.Image") -> float:
    """L 图像灰度标准差（用于对比度保持率的分母）。"""
    n, s1, s2 = _hist_moments(img_l)
    return _stddev_from_moments(n, s1, s2)


def _metrics(data: bytes, ref_l: Image.Image, orig_std: float):
    """解码候选字节流，返回 (PSNR, 对比度保持率)。

    PSNR 衡量像素级失真；对比度保持率（候选灰度标准差 / 源图标准差）衡量
    文字是否被「洗淡」——这是 PSNR 容易忽略、却决定可读性的关键指标。
    """
    try:
        dec = Image.open(io.BytesIO(data))
        if dec.mode != "L":
            dec = dec.convert("L")
        dec.load()
    except Exception:
        return 0.0, 0.0
    if dec.size != ref_l.size:
        try:
            dec = dec.resize(ref_l.size, Image.LANCZOS)
        except Exception:
            return 0.0, 0.0
    # 对比度保持率
    n, _s1, s2 = _hist_moments(dec)
    if not n:
        return 0.0, 0.0
    dn, _d1, d2 = _hist_moments(ImageChops.difference(ref_l, dec))
    if not dn:
        return 0.0, 0.0
    mse = d2 / max(1, dn)
    psnr = 99.0 if mse < 1e-6 else 10.0 * math.log10(255.0 * 255.0 / mse)
    cr = _stddev_from_moments(n, _s1, s2) / max(1e-6, orig_std)
    return psnr, cr


def _pareto_frontier(cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """求帕累托前沿（非支配解），按体积升序排列。

    一个候选 c 被支配，当且仅当存在另一个候选 o 体积更小且清晰度更高。
    先施加「清晰可读」约束：剔除 PSNR 低于下限、或对比度保持率过低的候选，
    保证前沿上的每个点都清晰可读（PNG 无损恒可读，不受下限约束）。
    """
    readable = [c for c in cands
                if c["name"] == "png"
                or (c["psnr"] >= _MIN_READABLE_PSNR
                    and c.get("contrast", 1.0) >= _MIN_CONTRAST_RATIO)]
    pool = readable if readable else cands

    frontier: List[Dict[str, Any]] = []
    for c in pool:
        dominated = any(
            (o["size"] <= c["size"] and o["psnr"] >= c["psnr"])
            and (o["size"] < c["size"] or o["psnr"] > c["psnr"])
            for o in pool)
        if not dominated:
            frontier.append(c)
    frontier.sort(key=lambda c: c["size"])
    return _resample_frontier(frontier, _FRONTIER_POINTS)


def _resample_frontier(frontier: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """把帕累托前沿重采样到最多 n 个点（按体积均匀分布，保留首尾端点）。"""
    if len(frontier) <= n:
        return frontier
    first, last = frontier[0], frontier[-1]
    interior = frontier[1:-1]
    need = n - 2
    if len(interior) <= need:
        return frontier
    step = (len(interior) - 1) / max(1, need - 1)
    idxs = sorted({int(round(i * step)) for i in range(need)})
    return [first] + [interior[i] for i in idxs] + [last]


def _select_by_position(frontier: List[Dict[str, Any]], position: float) -> Dict[str, Any]:
    """按「前沿位置」在前沿上选点：position ∈ [0,1]，0=最小体积，1=最高画质/源文件。

    前沿按体积升序排列，position 直接映射到前沿点的索引——这是「画质-体积轴」
    与帕累托前沿图共用的统一选点方式（一个系统，两种表现）。
    """
    if not frontier:
        raise ValueError("empty frontier")
    n = len(frontier)
    if n == 1:
        return frontier[0]
    idx = int(round(max(0.0, min(1.0, position)) * (n - 1)))
    return frontier[idx]


def _build_candidates(img: Image.Image, text_like: bool,
                      raw_size: int = 0) -> List[Dict[str, Any]]:
    """为单张位图生成多分辨率 × 编码器 × 质量的候选集，逐张计算体积与 PSNR。

    这是多目标优化的「搜索空间」：对每张图生成 JPEG（多质量）在多个缩放因子
    （分辨率）下的候选，加上 PNG 无损端点。无固定 DPI、无固定质量。

    raw_size 为原始流字节数，>0 时启用「必被 keep 支配」的精确剪枝。
    """
    ref_l = _reading_reference(img)
    orig_std = _stddev_l(ref_l)
    subsampling = 0 if text_like else 2
    w, h = img.size
    cands: List[Dict[str, Any]] = []

    # 全分辨率下用细粒度质量网格（保证帕累托前沿有足够多非支配点）；
    # 降采样档位用粗粒度，为「最小体积」端提供候选。
    # 仅使用 JPEG（有损）+ PNG（无损）两种通用编码，避免 JPEG 2000 在部分
    # PDF 阅读器中无法渲染导致「空白/黑点」的兼容性问题。
    for scale in (1.0, 0.6, 0.4):
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))
        base = img if scale >= 0.999 else img.resize((tw, th), Image.LANCZOS)
        if scale >= 0.999:
            # 细粒度质量网格（步长 3，覆盖 40~91），保证前沿有足够多的非支配点
            jpeg_qs = tuple(range(40, 94, 3))
        else:
            jpeg_qs = (50, 60, 70)
        for q in jpeg_qs:
            try:
                data = _jpeg_bytes(base, q, subsampling)
            except Exception:
                continue
            if raw_size and len(data) >= raw_size:
                # 精确剪枝：候选池里恒有 keep 端点（体积=raw、清晰度=99）。
                # 体积不小于原始流的 JPEG 必然被 keep 支配，绝不会进入帕累托
                # 前沿，其解码+缩放+差分+直方图测量可以整体跳过。
                # 结果与「先测量再被前沿筛掉」逐位一致，但省下该候选约 60%
                # 的计算量与内存占用。
                continue
            try:
                psnr, cr = _metrics(data, ref_l, orig_std)
                cands.append({"name": "jpeg", "size": len(data), "psnr": psnr,
                              "contrast": cr,
                              "payload": data, "width": tw, "height": th,
                              "resampled": scale < 1.0})
            except Exception:
                continue
    png = _encode_png_parts(img)
    if png:
        cands.append({"name": "png", "size": png["bytes"], "psnr": 99.0,
                      "contrast": 1.0,
                      "payload": png, "width": w, "height": h, "resampled": False})
    return cands


def _image_frontier(img: Image.Image, text_like: bool,
                    raw_size: int) -> List[Dict[str, Any]]:
    """唯一的多目标优化程序：对单张位图求帕累托前沿。

    生成「分辨率 × 编码器 × 质量」候选 + 「保留源文件」端点，求非支配解。
    界面上的帕累托前沿图与压缩过程都调用本函数，是同一个系统，无预览前后之分。
    """
    cands = _build_candidates(img, text_like, raw_size)
    w, h = img.size
    cands.append({"name": "keep", "size": raw_size, "psnr": 99.0, "contrast": 1.0,
                  "payload": None, "width": w, "height": h, "resampled": False})
    return _pareto_frontier(cands)


def _select_point(frontier: List[Dict[str, Any]], position: float) -> Dict[str, Any]:
    """在前沿上按位置选点（0=最小体积，1=最高画质/源文件）。"""
    return _select_by_position(frontier, position)


def _has_alpha(img: Image.Image) -> bool:
    """判断图像是否含有实际使用的透明通道（这类图不允许走 JPEG）。"""
    if img.mode in ("RGBA", "LA", "PA"):
        ext = img.getextrema()
        return bool(ext) and len(ext) == 4 and ext[3][0] < 255
    return "transparency" in img.info


def _is_text_like(img: Image.Image) -> bool:
    """检测文字/图表类图像（含大量锐利边缘）。

    这类图对 JPEG 振铃效应极其敏感，检测到时强制 4:4:4 色度采样并抬高
    质量下限，避免文字边缘出现彩色渗色。
    """
    try:
        w, h = img.size
        scale = min(1.0, 640.0 / max(1, w))
        small = img.convert("L")
        if scale < 1.0:
            small = small.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
        edges = small.filter(ImageFilter.FIND_EDGES)
        # 与「list(getdata()) 后统计 v>48 的像素占比」逐位等价，但避免把
        # 百万级像素实例化成 Python 列表（原本每张图约 40 ms + 大内存分配）。
        h = edges.histogram()
        total = sum(h)
        if not total:
            return False
        strong = sum(h[49:])
        return (strong / total) > 0.10
    except Exception:
        return False


def _is_effectively_grayscale(img: Image.Image) -> bool:
    """判断一张彩色位图是否实际为灰度（RGB 三通道几乎一致）。

    扫描件常以 RGB 存储但内容本就是黑白/灰度；转为灰度后 JPEG 体积可再减小
    约 2/3，且视觉上完全无损失。阈值取保守值，避免误伤真正彩色的图片。
    """
    try:
        rgb = img.convert("RGB")
        r, g, b = rgb.split()
        # 与 ImageStat.Stat(...).extrema[0][1] / .mean[0] 逐位等价：
        # extrema 的最大值 = 直方图最高非空桶；mean = 一阶和 / 像素数。
        for ch_a, ch_b in ((r, g), (g, b)):
            h = ImageChops.difference(ch_a, ch_b).histogram()
            n = sum(h)
            hi = 0
            s1 = 0.0
            for i, c in enumerate(h):
                if c:
                    hi = i
                    s1 += i * c
            if hi > 6 or (s1 / n if n else 0.0) >= 1.5:
                return False
        return True
    except Exception:
        return False


def _decode_image(obj) -> Optional[Image.Image]:
    """把位图 XObject 解码为 Pillow 图像；无法解码时返回 None。"""
    try:
        pim = pikepdf.PdfImage(obj)
        img = pim.as_pil_image()
        img.load()
        return img
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# 位图层优化（规划一次、压缩与分析共用 —— 保证页面显示体积 == 真实压缩体积）
# --------------------------------------------------------------------------------------
def _new_stats() -> Dict[str, Any]:
    return {"images": 0, "lossless_recompressed": 0, "lossy_recompressed": 0,
            "downsampled": 0, "kept": 0, "bytes_saved": 0,
            "gray_converted": 0, "jpx_recompressed": 0,
            "psnr_sum": 0.0, "psnr_count": 0, "psnr_min": 99.0,
            "frontier_points": [], "notes": []}


def _plan_image(key, obj) -> Dict[str, Any]:
    """对一张唯一位图执行与压缩时完全一致的分类与候选计算（结果可跨 Pdf 打开复用）。

    plan:
      {"key", "raw", "width", "height", "colorspace", "status", ...}
    status:
      "keep"     : 读取异常 / 过小 / 无法解码，压缩时原样保留（字节恒为 raw）；
      "fixed"    : 与 position 无关的固定选择（alpha 图在 keep / PNG 间取更小），
                   选中候选在 "cand"；
      "frontier" : 正常图，"frontier" 为帕累托候选列表。
    分类口径必须与压缩写回路径逐字一致，否则页面预估体积会与真实输出不符。
    """
    try:
        raw = obj.read_raw_bytes()
        width = int(obj.get("/Width"))
        height = int(obj.get("/Height"))
    except Exception:
        return {"key": key, "raw": b"", "width": 0, "height": 0,
                "colorspace": "rgb", "status": "keep"}
    if not raw or width < 8 or height < 8 or len(raw) < 2048:
        # 太小的图重编码收益极低，直接保留
        return {"key": key, "raw": raw, "width": width, "height": height,
                "colorspace": "gray", "status": "keep", "gray_converted": False}
    img = _decode_image(obj)
    if img is None:
        return {"key": key, "raw": raw, "width": width, "height": height,
                "colorspace": "gray", "status": "keep", "gray_converted": False}
    plan = _plan_pixels(key, len(raw), width, height, img)
    plan["raw"] = raw
    return plan


def _plan_pixels(key, raw_len: int, width: int, height: int,
                 img: Image.Image) -> Dict[str, Any]:
    """纯像素层规划：不触碰 pikepdf，可在子进程中执行。

    与 `_plan_image` 的分类口径逐字一致；返回的 plan 中 "raw" 为空字节，
    由调用方回填原始流（原始流不进 IPC，既省带宽也让主进程始终持有它）。
    """
    plan = {"key": key, "raw": b"", "width": width, "height": height,
            "colorspace": "gray", "status": "keep", "gray_converted": False}
    if img.mode in ("RGB", "RGBA"):
        try:
            if _is_effectively_grayscale(img):
                img = img.convert("L")
                plan["gray_converted"] = True
        except Exception:
            pass
    plan["colorspace"] = "gray" if img.mode in ("L", "1") else "rgb"
    if _has_alpha(img):
        # 含透明通道的图像不能走 JPEG，保留原数据流 与 PNG 无损重编码二选一取更小者。
        cands = [{"name": "keep", "size": raw_len, "tier": 0, "psnr": 99.0,
                  "payload": None, "resampled": False,
                  "width": width, "height": height}]
        png_orig = _encode_png_parts(img)
        if png_orig:
            cands.append({"name": "png", "size": png_orig["bytes"], "tier": 0,
                          "payload": png_orig, "resampled": False,
                          "width": width, "height": height, "psnr": 99.0})
        plan["status"] = "fixed"
        plan["cand"] = min(cands, key=lambda c: c["size"])
        return plan
    try:
        text_like = img.mode != "1" and _is_text_like(img)
    except Exception:
        text_like = False
    frontier = _image_frontier(img, text_like, raw_len)
    if not frontier:
        return plan
    plan["status"] = "frontier"
    plan["frontier"] = frontier
    return plan


# --------------------------------------------------------------------------------------
# 并行规划：把「逐张位图的候选搜索」分摊到多个 CPU 核心
# --------------------------------------------------------------------------------------
# 为什么必须用进程而不是线程：
#   JPEG 编码占候选搜索成本约 38%，libjpeg 在 C 层完成但**不释放 GIL**
#   （实测 16 线程 0.92x，即完全串行）；解码 / LANCZOS 缩放 / 差分 / 直方图
#   虽释放 GIL（3.5~7.8x），单靠线程只能压掉其中一半。只有进程级并行能同时
#   摊掉这两类成本。
# 为什么让子进程自己打开 PDF：
#   若由主进程解码后再把像素送过去，单本书就要跨进程搬运约 2 GB；改为子进程
#   各自 open + 只处理分到的若干张图，输入侧 IPC 近似为 0，且峰值内存可控。
_MP_DISABLED = False


def enable_parallel_support() -> None:
    """打包成 exe 后启用多进程并行。必须在 ``if __name__ == "__main__"`` 中调用。"""
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass


def _auto_workers() -> int:
    """根据 CPU 核数决定并行工作进程数（可用环境变量覆盖）。"""
    # 关闭开关优先于显式档位，保证 PDFC_DISABLE_MP=1 一定能退回单进程
    if (os.environ.get("PDFC_DISABLE_MP") or "").strip() == "1":
        return 1
    env = (os.environ.get("PDFC_WORKERS") or "").strip()
    if env.isdigit():
        try:
            return max(1, min(int(env), 32))
        except Exception:
            pass
    try:
        n = int(os.cpu_count() or 4)
    except Exception:
        n = 4
    # 留一核给主进程与界面响应；上限 16（实测 16 核后收益明显递减：
    # 16 核 8.6x → 31 核 10.2x，但峰值内存随核数线性上涨）
    return max(2, min(n - 1, 16))


def _plan_chunk_worker(job: Tuple[str, List[Any]]) -> List[Dict[str, Any]]:
    """进程/线程池 worker：自行打开 PDF，规划分配给自己的一组位图。

    每次调用独立打开与关闭 PDF（开销约 30 ms，相对单张图约 1.5 s 的搜索成本
    可忽略），好处是不必在 worker 生命周期内常驻文档对象，峰值内存可控。
    """
    src_path, keys = job
    out: List[Dict[str, Any]] = []
    try:
        with pikepdf.open(src_path) as pdf:
            _refs, samples = collect_image_refs(pdf)
            for k in keys:
                obj = samples.get(k)
                if obj is None:
                    continue
                try:
                    out.append(_plan_image(k, obj))
                except Exception as exc:
                    out.append({"key": k, "raw": b"", "width": 0, "height": 0,
                                "colorspace": "gray", "status": "keep",
                                "gray_converted": False,
                                "error": "%s: %s" % (type(exc).__name__, exc)})
    except Exception:
        return out
    return out


def _split_keys(keys: List[Any], n_chunks: int) -> List[List[Any]]:
    """按键轮询切分成 n_chunks 组（轮询而非连续分段，避免大图扎堆）。"""
    chunks: List[List[Any]] = [[] for _ in range(max(1, n_chunks))]
    for i, k in enumerate(keys):
        chunks[i % len(chunks)].append(k)
    return [c for c in chunks if c]


def _plan_images_parallel(src_path: str, keys: List[Any], workers: int,
                          progress: Optional[Callable[[float, str], None]]
                          ) -> Optional[List[Dict[str, Any]]]:
    """用进程池并行规划；失败返回 None（由调用方降级到线程/串行）。"""
    global _MP_DISABLED
    if _MP_DISABLED:
        return None
    chunks = _split_keys(keys, workers * 4)
    plans: List[Dict[str, Any]] = []
    try:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futs = [ex.submit(_plan_chunk_worker, (src_path, c)) for c in chunks]
            done = 0
            for f in as_completed(futs):
                got = f.result()
                if got:
                    plans.extend(got)
                done += 1
                if progress:
                    progress(4 + int(80 * done / max(1, len(chunks))),
                             "正在计算位图前沿 %d/%d" % (done, len(chunks)))
    except Exception as exc:
        # 进程池不可用（受限环境 / 打包异常 / 内存不足）时永久降级，不再重试
        _MP_DISABLED = True
        _ = exc
        return None
    return plans


def _plan_images_threaded(src_path: str, keys: List[Any], workers: int,
                          progress: Optional[Callable[[float, str], None]]
                          ) -> List[Dict[str, Any]]:
    """线程池兜底：JPEG 编码仍串行，但解码/缩放/差分/直方图可并发，约 1.9x。"""
    chunks = _split_keys(keys, workers * 4)
    plans: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_plan_chunk_worker, (src_path, c)) for c in chunks]
        done = 0
        for f in as_completed(futs):
            got = f.result()
            if got:
                plans.extend(got)
            done += 1
            if progress:
                progress(4 + int(80 * done / max(1, len(chunks))),
                         "正在计算位图前沿 %d/%d" % (done, len(chunks)))
    return plans


def _plan_images_serial(samples: Dict[Any, Any],
                        progress: Optional[Callable[[float, str], None]]
                        ) -> List[Dict[str, Any]]:
    """串行规划（最低保障路径）。"""
    keys = list(samples.keys())
    total = max(1, len(keys))
    plans: List[Dict[str, Any]] = []
    for i, k in enumerate(keys):
        if progress:
            progress(4 + int(80 * i / total),
                     "正在计算位图前沿 %d/%d" % (i + 1, total))
        plans.append(_plan_image(k, samples[k]))
    return plans


def _plan_all(src_path: Optional[str], samples: Dict[Any, Any],
              progress: Optional[Callable[[float, str], None]] = None
              ) -> List[Dict[str, Any]]:
    """规划全部唯一位图：优先进程并行，逐级降级到线程、串行。"""
    keys = list(samples.keys())
    if not keys:
        return []

    def _complete(plans: List[Dict[str, Any]]) -> bool:
        return bool(plans) and len({str(p.get("key")) for p in plans}) >= len(
            {str(k) for k in keys})

    workers = _auto_workers()
    # 工作量门槛：并行启动本身约 1~2 s，小文档并行反而更慢。
    # 串行吞吐实测约 0.24 s / Mpx，取 25 Mpx 作为收支平衡点。
    total_px = 0
    for o in samples.values():
        try:
            total_px += int(o.get("/Width")) * int(o.get("/Height"))
        except Exception:
            pass
    can_parallel = (
        workers > 1 and len(keys) >= 8 and total_px >= 25_000_000
        and bool(src_path) and os.path.exists(src_path))
    if can_parallel:
        got = _plan_images_parallel(src_path, keys, workers, progress)
        if got is not None and _complete(got):
            return got
    if can_parallel:
        try:
            got = _plan_images_threaded(src_path, keys, workers, progress)
            if _complete(got):
                return got
        except Exception:
            pass
    return _plan_images_serial(samples, progress)


def _plan_selected(plan: Dict[str, Any], position: float) -> Dict[str, Any]:
    """按全局 position 从 plan 中选出最终候选（与压缩选点同口径）。"""
    status = plan["status"]
    if status == "keep":
        return {"name": "keep", "size": len(plan["raw"]), "tier": 0,
                "psnr": 99.0, "payload": None,
                "width": plan["width"], "height": plan["height"]}
    if status == "fixed":
        return plan["cand"]
    return _select_by_position(plan["frontier"], position)


def _final_stream_data(plan: Dict[str, Any], cand: Dict[str, Any]) -> bytes:
    """候选在最终 PDF 中对应的原始流字节（用于模拟去重与体积汇总）。"""
    name = cand["name"]
    if name == "keep":
        return plan["raw"]
    if name == "png":
        return cand["payload"]["data"]
    return cand["payload"]


def _build_stream(pdf, obj, plan: Dict[str, Any], cand: Dict[str, Any]):
    """按候选构建新的位图 stream 对象（不负责替换引用）。"""
    width, height = plan["width"], plan["height"]
    if cand["name"] == "png":
        payload = cand["payload"]
        layout = payload["layout"]
        new_stream = pdf.make_stream(payload["data"])
        new_stream.Filter = pikepdf.Name("/FlateDecode")
        new_stream.DecodeParms = pikepdf.Dictionary(
            Predictor=15,
            Colors=layout["colors"],
            BitsPerComponent=8,
            Columns=layout["width"],
        )
        new_stream.Width = layout["width"]
        new_stream.Height = layout["height"]
        new_stream.BitsPerComponent = 8
        new_stream.ColorSpace = (
            pikepdf.Name("/DeviceGray") if layout["colors"] == 1
            else pikepdf.Name("/DeviceRGB")
        )
        if payload["smask"]:
            sl = payload["smask"]["layout"]
            sm = pdf.make_stream(payload["smask"]["data"])
            sm.Filter = pikepdf.Name("/FlateDecode")
            sm.DecodeParms = pikepdf.Dictionary(
                Predictor=15, Colors=1, BitsPerComponent=8, Columns=sl["width"]
            )
            sm.Width = sl["width"]
            sm.Height = sl["height"]
            sm.BitsPerComponent = 8
            sm.ColorSpace = pikepdf.Name("/DeviceGray")
            sm.Subtype = pikepdf.Name("/Image")
            new_stream.SMask = pdf.make_indirect(sm)
        else:
            # 原图自带的遮罩/透明信息需要原样保留
            for k in ("/SMask", "/Mask"):
                try:
                    if k in obj:
                        new_stream[k] = obj[k]
                except Exception:
                    pass
    else:
        new_stream = pdf.make_stream(cand["payload"])
        if cand["name"] == "jpx":
            new_stream.Filter = pikepdf.Name("/JPXDecode")
        else:
            new_stream.Filter = pikepdf.Name("/DCTDecode")
        new_stream.Width = cand.get("width", width)
        new_stream.Height = cand.get("height", height)
        new_stream.BitsPerComponent = 8
        new_stream.ColorSpace = (
            pikepdf.Name("/DeviceGray") if plan["colorspace"] == "gray"
            else pikepdf.Name("/DeviceRGB")
        )
        for k in ("/SMask", "/Mask"):
            try:
                if k in obj:
                    new_stream[k] = obj[k]
            except Exception:
                pass

    new_stream.Subtype = pikepdf.Name("/Image")
    try:
        if "/Interpolate" in obj:
            new_stream["/Interpolate"] = obj["/Interpolate"]
    except Exception:
        pass
    return new_stream


def _apply_plans(pdf, plans, profile: Profile,
                 progress: Optional[Callable[[float, str], None]] = None) -> Dict[str, Any]:
    """在已打开的 pdf 上按 plans 执行替换并返回统计（压缩与分析共用同一路径）。"""
    stats = _new_stats()

    def note(msg: str) -> None:
        if len(stats["notes"]) < 12:
            stats["notes"].append(msg)

    # 收集位图及其全部引用点（可能被多页面共享）
    refs, samples = collect_image_refs(pdf)
    stats["images"] = len(samples)
    plan_by_key = {p["key"]: p for p in plans}

    # 汇总各页面中的显示尺寸（仅用于诊断说明）
    placements: Dict[Any, Tuple[float, float]] = {}
    page_width_pt = 595.0
    try:
        for page in pdf.pages:
            for k, v in _placement_map(page).items():
                prev = placements.get(k, (0.0, 0.0))
                placements[k] = (max(prev[0], v[0]), max(prev[1], v[1]))
        page_width_pt = float(pdf.pages[0].get("/MediaBox")[2]) or 595.0
    except Exception:
        pass

    total = max(1, len(samples))
    for idx, (key, obj) in enumerate(samples.items()):
        if progress:
            progress(8 + int(70 * idx / total),
                     "正在优化位图 %d/%d" % (idx + 1, total))
        plan = plan_by_key.get(key)
        if plan is None:
            plan = _plan_image(key, obj)  # 兜底：预计算缺失时现场计算
        raw = plan["raw"]
        cand = _plan_selected(plan, profile.position)
        cand["tier"] = 2 if cand["name"] in ("jpeg", "jpx") else 0

        if plan["status"] == "frontier":
            for c in plan["frontier"]:
                stats["frontier_points"].append(
                    {"size": int(c["size"]), "psnr": round(c["psnr"], 1)})
        if plan.get("gray_converted"):
            stats["gray_converted"] += 1

        if cand.get("psnr") is not None:
            stats["psnr_sum"] += float(cand["psnr"])
            stats["psnr_count"] += 1
            stats["psnr_min"] = min(stats["psnr_min"], float(cand["psnr"]))

        disp_w, disp_h = placements.get(key, (0.0, 0.0))
        if disp_w <= 1.0:
            disp_w = page_width_pt
            disp_h = page_width_pt * plan["height"] / max(1, plan["width"])
        eff_dpi = plan["width"] / (disp_w / 72.0) if disp_w > 1 else 96.0

        if cand["name"] == "keep" or cand["size"] >= len(raw):
            stats["kept"] += 1
            note("obj%s 保留：原始 %d B，最优 %s %d B，有效 %d DPI"
                 % (str(key), len(raw), cand["name"], cand["size"], int(eff_dpi)))
            continue

        # ---------- 写回 PDF ----------
        try:
            new_stream = _build_stream(pdf, obj, plan, cand)
            # 把所有引用点都指向新数据流，旧数据流才会被真正回收
            placed = 0
            for c2, n2 in refs.get(key, []):
                try:
                    c2.get("/XObject")[n2] = new_stream
                    placed += 1
                except Exception:
                    pass
            if placed == 0:
                stats["kept"] += 1
                note("位图 obj%s 没有可写入的引用点" % str(key))
                continue
        except Exception as exc:
            stats["kept"] += 1
            note("位图 obj%s 写回失败：%s: %s" % (str(key), type(exc).__name__, exc))
            continue

        stats["bytes_saved"] += len(raw) - cand["size"]
        if cand["tier"] == 2:
            stats["lossy_recompressed"] += 1
            if cand["name"] == "jpx":
                stats["jpx_recompressed"] += 1
        else:
            stats["lossless_recompressed"] += 1
        if cand.get("resampled"):
            stats["downsampled"] += 1

    return stats


def _optimize_images(
    pdf: "pikepdf.Pdf",
    profile: Profile,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """位图层优化：现场规划 + 应用（与分析预计算路径完全一致）。"""
    _refs, samples = collect_image_refs(pdf)
    src_path = None
    try:
        src_path = pdf.filename
    except Exception:
        src_path = None
    plans = _plan_all(src_path, samples, progress)
    return _apply_plans(pdf, plans, profile, progress)


# --------------------------------------------------------------------------------------
# 结构优化（完全无损）
# --------------------------------------------------------------------------------------
def _dedupe_images(pdf: "pikepdf.Pdf") -> int:
    """把内容完全相同的重复位图合并为同一个对象，返回合并数量。"""
    seen: Dict[bytes, Any] = {}
    removed = 0
    for container, name, obj in iter_image_xobjects(pdf):
        try:
            raw = obj.read_raw_bytes()
        except Exception:
            continue
        digest = hashlib.blake2b(raw, digest_size=16).digest()
        if digest in seen:
            try:
                container.get("/XObject")[name] = seen[digest]
                removed += 1
            except Exception:
                pass
        else:
            seen[digest] = obj
    return removed


def _drop_thumbnails(pdf: "pikepdf.Pdf") -> int:
    """删除页面内嵌缩略图（正文不使用它，属于冗余数据），返回删除数量。"""
    dropped = 0
    try:
        for page in pdf.pages:
            try:
                if "/Thumb" in page:
                    del page["/Thumb"]
                    dropped += 1
            except Exception:
                pass
    except Exception:
        pass
    return dropped


def _strip_metadata(pdf: "pikepdf.Pdf") -> None:
    """清除 XMP 元数据与文档信息（作者、标题、软件等），不影响页面内容。"""
    try:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            for k in list(meta.keys()):
                try:
                    del meta[k]
                except Exception:
                    pass
    except Exception:
        pass
    try:
        del pdf.Root.Metadata
    except Exception:
        pass
    try:
        info = pdf.trailer.get("/Info")
        if info is not None:
            for k in list(info.keys()):
                try:
                    del info[k]
                except Exception:
                    pass
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# 压缩主流程
# --------------------------------------------------------------------------------------
def _compress_core(
    src_path: str,
    dst_path: str,
    profile: Profile,
    plans: Optional[List[Dict[str, Any]]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    t0 = time.time()
    if progress:
        progress(2, "正在分析文档结构")

    original_size = os.path.getsize(src_path)

    # 临时目录存放中间结果，处理完成后无论成功失败都删除
    tmp_dir = tempfile.mkdtemp(prefix="pdfc_")
    stage = os.path.join(tmp_dir, "stage.pdf")
    try:
        with pikepdf.open(src_path) as pdf:
            src_pages = len(pdf.pages)
            if progress:
                progress(6, "正在读取页面与位图")
            if plans is None:
                # 复用缓存的 plans（分析阶段已算过则不再重复计算，结果完全一致）
                plans = get_plans(src_path, progress)
            stats = _apply_plans(pdf, plans, profile, progress)

            if progress:
                progress(80, "正在重组对象并清理冗余")
            if profile.dedupe:
                stats["deduped"] = _dedupe_images(pdf)
            if profile.drop_thumbnails:
                stats["thumbnails_dropped"] = _drop_thumbnails(pdf)
            if profile.strip_metadata:
                _strip_metadata(pdf)
            try:
                pdf.remove_unreferenced_resources()
            except Exception:
                pass

            if progress:
                progress(90, "正在写入结果文件")
            # recompress_flate 必须保持 False：否则 pikepdf 会把上面精心构建的
            # 预测器无损数据流解码后重新编码，并丢弃 DecodeParms，导致文件膨胀。
            pdf.save(
                stage,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                compress_streams=True,
                recompress_flate=False,
                normalize_content=False,
                linearize=False,
            )

        if progress:
            progress(94, "正在校验输出文件完整性")
        out_size = os.path.getsize(stage)
        # 完整性校验：输出页数必须与原文件一致
        ok = True
        try:
            with pikepdf.open(stage) as chk:
                ok = len(chk.pages) == src_pages
        except Exception:
            ok = False
        if not ok:
            raise RuntimeError("输出文件校验失败（原文件未被修改）")

        if out_size < original_size:
            shutil.copyfile(stage, dst_path)
            fallback = False
            final_size = out_size
        else:
            # 压缩后反而变大，说明原文件已优化充分，直接交付原文件副本
            shutil.copyfile(src_path, dst_path)
            fallback = True
            final_size = original_size

        return {
            "ok": True,
            "profile": profile.label,
            "original_size": original_size,
            "final_size": final_size,
            "saved_bytes": max(0, original_size - final_size),
            "saved_percent": round(max(0.0, (1 - final_size / max(1, original_size)) * 100), 2),
            "pages": src_pages,
            "image_count": stats.get("images", 0),
            "lossless_recompressed": stats.get("lossless_recompressed", 0),
            "lossy_recompressed": stats.get("lossy_recompressed", 0),
            "downsampled": stats.get("downsampled", 0),
            "gray_converted": stats.get("gray_converted", 0),
            "jpx_recompressed": stats.get("jpx_recompressed", 0),
            "kept": stats.get("kept", 0),
            "deduped": stats.get("deduped", 0),
            "thumbnails_dropped": stats.get("thumbnails_dropped", 0),
            "psnr": round(stats.get("psnr_min", 99.0), 1),
            "psnr_avg": round(stats.get("psnr_sum", 0.0) / max(1, stats.get("psnr_count", 0)), 1),
            "frontier_points": stats.get("frontier_points", []),
            "fallback_kept_original": fallback,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def compress_pdf(
    src_path: str,
    dst_path: str,
    profile: Profile,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    return _compress_core(src_path, dst_path, profile, None, progress)


def precompute_pdf(
    src_path: str,
    progress: Optional[Callable[[float, str], None]] = None,
) -> List[Dict[str, Any]]:
    """为全部唯一位图生成可复用 plans（成本约等于一次压缩的图像阶段）。"""
    with pikepdf.open(src_path) as pdf:
        _refs, samples = collect_image_refs(pdf)
        return _plan_all(src_path, samples, progress)


# --------------------------------------------------------------------------------------
# 规划结果缓存：分析一次，压缩复用
# --------------------------------------------------------------------------------------
# 「添加文件即生成帕累托前沿图」与「点开始压缩」走的是同一套 plans，但原本各算
# 一遍（各约 230 s）。plans 只取决于文件字节内容，与档位/滑杆无关，因此可以
# 安全地跨调用复用 —— 这是纯粹的重复计算消除，不改变任何压缩结果。
_PLAN_CACHE: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
_PLAN_CACHE_MAX = 1  # 只缓存最近一个文件，避免大文档下占用过多内存


def _plan_cache_key(src_path: str) -> str:
    """文件身份指纹：路径 + 大小 + 修改时间（内容变化即失效）。"""
    try:
        norm = os.path.normcase(os.path.abspath(src_path))
        st = os.stat(src_path)
        return "%s|%d|%d" % (norm, st.st_size, int(st.st_mtime))
    except Exception:
        return os.path.normcase(os.path.abspath(src_path))


def get_plans(src_path: str,
              progress: Optional[Callable[[float, str], None]] = None
              ) -> List[Dict[str, Any]]:
    """取得（必要时计算并缓存）某文件的全部位图 plans。"""
    ck = _plan_cache_key(src_path)
    cached = _PLAN_CACHE.get(ck)
    if cached is not None:
        _PLAN_CACHE.move_to_end(ck)
        return cached
    plans = precompute_pdf(src_path, progress)
    _PLAN_CACHE[ck] = plans
    while len(_PLAN_CACHE) > _PLAN_CACHE_MAX:
        _PLAN_CACHE.popitem(last=False)
    return plans


def compress_pdf_planned(
    src_path: str,
    dst_path: str,
    plans: List[Dict[str, Any]],
    profile: Profile,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """用预计算 plans 压缩（多点验证时避免重复生成候选，只做替换与保存）。"""
    return _compress_core(src_path, dst_path, profile, plans, progress)


# --------------------------------------------------------------------------------------
# 体积约束：目标体积 + 自动逐级增强 + 超限说明
# --------------------------------------------------------------------------------------
# 档位按压缩强度从弱到强排列。当设定了目标体积（target_bytes）且所选档位无法
# 满足时，自动沿此顺序逐级增强；"extreme" 是最后手段，不对外展示。
_AGGRESSIVENESS = ("high", "balanced", "smallest", "extreme")


def escalation_chain(start_key: str) -> List[str]:
    """返回从 start_key 开始、强度递增的档位序列。

    允许逐级增强到 "extreme"（帕累托前沿上体积最优的点）。
    """
    try:
        idx = _AGGRESSIVENESS.index(start_key)
    except ValueError:
        idx = 0
    return list(_AGGRESSIVENESS[idx:])


def _escalation_profiles(profile: Profile) -> List[Profile]:
    """返回从用户当前 profile 开始、压缩强度严格递增的 Profile 列表。

    第 1 个元素是 profile 的副本（保留滑杆给出的自定义 position，必须原样
    使用，禁止按 key 重建而丢掉 position）；其后按 position 从高到低接入压缩
    更强的预设档（高画质 0.7 → 均衡 0.4 → 最小体积 0.05 → 极限 0.0），
    仅纳入 position 严格小于当前值的档位，避免重复尝试同强度点。
    """
    ladder = (PRESETS["high"], PRESETS["balanced"],
              PRESETS["smallest"], PRESETS["extreme"])
    chain = [dataclasses.replace(profile)]
    for cand in ladder:
        if cand.position < profile.position - 1e-9:
            chain.append(dataclasses.replace(
                cand, strip_metadata=profile.strip_metadata))
    return chain


def analyze_breakdown(src_path: str) -> Dict[str, Any]:
    """粗略统计 PDF 各类数据的字节占比，用于解释“为何无法进一步压缩”。

    返回 {images, fonts, embedded, other}，单位字节。
    """
    result = {"images": 0, "fonts": 0, "embedded": 0, "other": 0}
    try:
        with pikepdf.open(src_path) as pdf:
            for obj in pdf.objects:
                if not isinstance(obj, pikepdf.Stream):
                    continue
                try:
                    size = len(obj.read_raw_bytes())
                except Exception:
                    continue
                subtype = str(obj.get("/Subtype", ""))
                typ = str(obj.get("/Type", ""))
                if subtype == "/Image":
                    result["images"] += size
                elif typ in ("/Font", "/FontDescriptor") or any(
                        k in obj.keys() for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    result["fonts"] += size
                elif typ == "/EmbeddedFile" or "/EF" in obj.keys():
                    result["embedded"] += size
                else:
                    result["other"] += size
    except Exception:
        pass
    return result


def _explain_over_limit(src_path: str, report: Dict[str, Any],
                        target_bytes: int) -> Tuple[str, List[str]]:
    """生成“超过目标体积”的原因说明与进一步优化建议。"""
    br = analyze_breakdown(src_path)
    total = max(1, sum(br.values()))

    def pct_str(num: int) -> str:
        p = num / total * 100
        return "<1%" if 0 < p < 1 else "%.0f%%" % p

    parts = []
    if br["images"]:
        parts.append("位图占 %s（%s）" % (pct_str(br["images"]), human_size(br["images"])))
    if br["fonts"]:
        parts.append("内嵌字体占 %s（%s）" % (pct_str(br["fonts"]), human_size(br["fonts"])))
    if br["embedded"]:
        parts.append("内嵌附件占 %s（%s）" % (pct_str(br["embedded"]), human_size(br["embedded"])))
    if br["other"]:
        parts.append("矢量内容/结构数据占 %s（%s）" % (pct_str(br["other"]), human_size(br["other"])))

    reason_parts = []
    kept = report.get("kept", 0)
    img_cnt = report.get("image_count", 0)
    if img_cnt and kept >= img_cnt:
        reason_parts.append(
            "全部 %d 张位图均为无法进一步压缩的格式（如 JBIG2/JPX 或已高度压缩的 JPEG）" % img_cnt)
    elif kept:
        reason_parts.append(
            "%d 张位图因格式受限（JBIG2/JPX 等）无法进一步压缩" % kept)
    if br["fonts"] > total * 0.3:
        reason_parts.append("文档内嵌字体体积较大，有损压缩无法作用于字体数据")
    if br["embedded"]:
        reason_parts.append("文档包含内嵌附件，其内容无法被位图压缩处理")
    if not reason_parts:
        reason_parts.append("文件主要内容为矢量与已优化数据，位图压缩空间已用尽")

    reason = "目标 %s 未达到：%s。" % (
        human_size(target_bytes),
        "；".join(parts) + "。" + "；".join(reason_parts) if parts else "；".join(reason_parts))

    suggestions = [
        "已自动使用最强压缩档位（帕累托前沿上体积最优的点），位图侧已无进一步压缩空间。",
    ]
    if br["fonts"]:
        suggestions.append(
            "内嵌字体占比较大：建议在源文档中启用字体子集化，或导出时去除未用字体。")
    if br["embedded"]:
        suggestions.append("可删除内嵌附件（不影响正文）以减小体积。")
    suggestions.append("可考虑拆分文档（共 %d 页），或仅压缩真正需要的页面。" % report.get("pages", 0))
    suggestions.append("如需更小的文件，请在源编辑器中降低原始扫描分辨率后重新生成 PDF。")
    return reason, suggestions


def compress_pdf_bounded(
    src_path: str,
    dst_path: str,
    profile: Profile,
    target_bytes: Optional[int] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """在满足体积约束的前提下压缩 PDF。

    - target_bytes 为 None 时，行为与 compress_pdf 一致（单次压缩，严格按
      profile.position 选点）。
    - 设置了 target_bytes 时，先按当前 profile（含滑杆自定义 position）压缩；
      若产物仍超过目标，则沿 position 从高到低（高画质 0.7 → 均衡 0.4 →
      最小体积 0.05 → 极限 0.0）逐级增强重试，取满足约束的最小产物。
    - 若最强档位仍无法满足约束，返回超限原因与进一步优化建议
      （report["within_limit"] 为 False）。
    """
    # 逐级增强链：第 1 个就是用户当前 profile（含滑杆自定义 position，必须
    # 原样使用）；其后按 position 从高到低接入压缩更强的预设档。
    chain = _escalation_profiles(profile)
    n = len(chain)
    best_report: Optional[Dict[str, Any]] = None
    tried: List[Dict[str, Any]] = []

    # 中间结果写到系统临时目录，避免污染输出目录；处理结束统一清理。
    tmp_dir = tempfile.mkdtemp(prefix="pdfc_bound_")
    cur_tmp = os.path.join(tmp_dir, "cur.pdf")
    best_tmp = os.path.join(tmp_dir, "best.pdf")
    try:
        for i, p in enumerate(chain):
            key = p.key

            def wrapped(pct: float, msg: str, _i: int = i, _n: int = n) -> None:
                if progress:
                    progress((_i + max(0.0, min(100.0, pct)) / 100.0) / _n * 100, msg)

            report = compress_pdf(src_path, cur_tmp, p, wrapped)
            report["profile_key"] = key
            tried.append({"key": key, "label": p.label, "final_size": report["final_size"]})

            if best_report is None or report["final_size"] < best_report["final_size"]:
                best_report = report
                shutil.copyfile(cur_tmp, best_tmp)

            if target_bytes is None:
                break
            if report["final_size"] <= target_bytes:
                break
            if i == n - 1:
                break

        assert best_report is not None
        # 把最优结果写入最终目标路径
        shutil.copyfile(best_tmp, dst_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    best_report["target_bytes"] = target_bytes
    best_report["tried_profiles"] = tried
    best_report["within_limit"] = (
        target_bytes is None or best_report["final_size"] <= target_bytes)

    if target_bytes is not None and not best_report["within_limit"]:
        reason, suggestions = _explain_over_limit(src_path, best_report, target_bytes)
        best_report["limit_reason"] = reason
        best_report["suggestions"] = suggestions
    return best_report


# --------------------------------------------------------------------------------------
# 输出校验：验证压缩后的 PDF 是否「正确」
# --------------------------------------------------------------------------------------
def _page_images(page) -> List[Image.Image]:
    """提取一页中所有位图的解码图像（按资源字典顺序）。"""
    out: List[Image.Image] = []
    try:
        res = page.get("/Resources")
        xobjs = res.get("/XObject") if res is not None else None
    except Exception:
        return out
    if xobjs is None:
        return out
    for name in list(xobjs.keys()):
        try:
            obj = xobjs[name]
            if str(obj.get("/Subtype")) != "/Image":
                continue
            im = _decode_image(obj)
            if im is not None:
                out.append(im)
        except Exception:
            continue
    return out


def verify_compression(src_path: str, dst_path: str,
                       psnr_floor: float = 24.0,
                       contrast_floor: float = 0.6) -> Dict[str, Any]:
    """校验压缩输出 PDF 是否「正确」（结构与内容双重校验）。

    - 结构：页数一致、每页位图数量一致；
    - 内容：每张位图能解码，且相对源图 PSNR / 对比度保持率不低于下限
      （杜绝「压成空白/发灰不可读」的错误输出）。
    返回 report["ok"] 为 True 表示通过校验。
    """
    report: Dict[str, Any] = {
        "ok": True, "pages_match": False, "src_pages": 0, "dst_pages": 0,
        "images_checked": 0, "failed_images": 0, "min_psnr": 99.0,
        "min_contrast": 1.0, "errors": [],
    }
    try:
        with pikepdf.open(src_path) as src, pikepdf.open(dst_path) as dst:
            report["src_pages"] = len(src.pages)
            report["dst_pages"] = len(dst.pages)
            report["pages_match"] = report["src_pages"] == report["dst_pages"]
            if not report["pages_match"]:
                report["ok"] = False
                report["errors"].append(
                    "页数不一致：源 %d vs 输出 %d" % (report["src_pages"], report["dst_pages"]))

            for i, (sp, dp) in enumerate(zip(src.pages, dst.pages)):
                a_imgs = _page_images(sp)
                b_imgs = _page_images(dp)
                if len(a_imgs) != len(b_imgs):
                    report["ok"] = False
                    report["errors"].append(
                        "第 %d 页位图数量不一致：源 %d vs 输出 %d" % (i, len(a_imgs), len(b_imgs)))
                    continue
                for a, b in zip(a_imgs, b_imgs):
                    ref = _reading_reference(a)
                    std = ImageStat.Stat(ref).stddev[0]
                    buf = io.BytesIO()
                    b.convert("L").save(buf, "PNG")
                    psnr, cr = _metrics(buf.getvalue(), ref, std)
                    report["images_checked"] += 1
                    report["min_psnr"] = min(report["min_psnr"], psnr)
                    report["min_contrast"] = min(report["min_contrast"], cr)
                    if psnr < psnr_floor or cr < contrast_floor:
                        report["failed_images"] += 1
                        report["ok"] = False
                        report["errors"].append(
                            "第 %d 页位图退化：PSNR %.1f dB / 对比度 %.2f" % (i, psnr, cr))
    except Exception as e:
        report["ok"] = False
        report["errors"].append("校验异常：%s: %s" % (type(e).__name__, e))
    return report


# --------------------------------------------------------------------------------------
# 聚合帕累托前沿分析（页面显示体积必须与真实压缩体积一一对应）
# --------------------------------------------------------------------------------------
def analyze_pdf_frontier(src_path: str,
                         n_points: int = _AGGREGATE_POINTS,
                         progress: Optional[Callable[[float, str], None]] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """返回 PDF 的精确聚合帕累托前沿（体积 vs 清晰度），供界面图表绘制。

    严格一一对应，不做任何估算/外推：界面上每个点的体积与清晰度，都由该位置
    的一次**真实压缩**得到（与用户点下「开始压缩」后执行的是同一条 planned
    压缩路径），因此页面显示体积 == 用户最终拿到文件的真实体积，误差恒为 0。

      1. 对全部唯一位图预计算 plans（成本主要在候选 JPEG 编码，仅做一次）；
      2. 对 n_points 个位置逐个执行真实 planned 压缩（仅替换流与保存，
         test_book 规模下 44 点合计约 9 秒）；
      3. 每个点直接采用真实压缩报告的 final_size 与平均 PSNR。

    返回 (points, src_size)：
    - points：按体积升序的 n 个点 {size, psnr, position}，
      size 即该位置真实压缩产物的字节数；
    - src_size：源文件实际字节（界面红色源点）。
    """
    plans = get_plans(src_path, progress)
    file_size = os.path.getsize(src_path)

    points: List[Dict[str, Any]] = []
    tmp_dir = tempfile.mkdtemp(prefix="pdfc_front_")
    try:
        for i in range(n_points):
            pos = i / max(1, n_points - 1)
            profile = build_profile("balanced")
            profile.position = pos
            dst = os.path.join(tmp_dir, "point_%02d.pdf" % i)
            if progress:
                progress(92 + int(6 * i / max(1, n_points - 1)),
                         "正在真实校准前沿点 %d/%d" % (i + 1, n_points))
            r = compress_pdf_planned(src_path, dst, plans, profile)
            points.append({
                "size": int(r["final_size"]),
                "psnr": round(float(r["psnr_avg"]), 1),
                "position": pos,
            })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    points.sort(key=lambda p: p["size"])
    return points, file_size
