"""Small, dependency-free STL/OBJ to DLP mask and G-code slicer for HKUprint.

Each export is a self-contained print job: ``run.gcode`` plus one grayscale PNG
mask per layer. It is intentionally a contour slicer, not a full mesh repair
engine; invalid or self-intersecting meshes should be repaired upstream.
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import tempfile
import time
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


EPS = 1e-7
MERGE_EPS = 1e-6
MAX_FILL_RINGS = 4096
MASK_WIDTH = 3840
MASK_HEIGHT = 2160
MACHINE_X = 80.0
MACHINE_Y = 45.0
MACHINE_Z = 55.0


@dataclass(slots=True)
class Vertex:
    x: float
    y: float
    z: float


@dataclass(slots=True)
class Triangle:
    a: Vertex
    b: Vertex
    c: Vertex


@dataclass(slots=True)
class Mesh:
    triangles: list[Triangle] = field(default_factory=list)

    @property
    def bounds(self) -> tuple[float, float, float, float, float, float]:
        points = [p for t in self.triangles for p in (t.a, t.b, t.c)]
        if not points:
            raise ValueError("模型没有三角面")
        return (
            min(p.x for p in points), max(p.x for p in points),
            min(p.y for p in points), max(p.y for p in points),
            min(p.z for p in points), max(p.z for p in points),
        )


@dataclass(slots=True)
class SliceLayer:
    number: int
    z: float
    material: str
    paths: list[list[tuple[float, float]]]


@dataclass(slots=True)
class Settings:
    layer_height: float = 0.05
    first_layer_height: float = 0.05
    gap: float = 1.0
    fill_enabled: bool = False
    fill_spacing: float = 1.0
    feed_speed: float = 65.0
    travel_speed: float = 150.0
    xy_feed_speed: float = 180.0
    xy_travel_speed: float = 1200.0
    bed_x: float = MACHINE_X
    bed_y: float = MACHINE_Y
    margin: float = 3.0
    support_layers: int = 8
    support_dwell: float = 50.0
    exposure: float = 6.0
    a_layers: int = 0
    auto_scale: bool = True
    center_model: bool = True
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    rotation_z: float = 0.0
    slice_direction: tuple[float, float, float] = (0.0, 0.0, 1.0)


def _float_triplet(parts: list[str]) -> Vertex:
    return Vertex(float(parts[0]), float(parts[1]), float(parts[2]))


def load_obj(path: Path) -> Mesh:
    vertices: list[Vertex] = []
    triangles: list[Triangle] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].lower() == "v" and len(parts) >= 4:
            vertices.append(_float_triplet(parts[1:4]))
        elif parts[0].lower() == "f" and len(parts) >= 4:
            indexes = [int(item.split("/")[0]) for item in parts[1:]]
            indexes = [index - 1 if index > 0 else len(vertices) + index for index in indexes]
            if any(index < 0 or index >= len(vertices) for index in indexes):
                raise ValueError(f"OBJ 面索引超出范围: {line}")
            for index in range(1, len(indexes) - 1):
                triangles.append(Triangle(vertices[indexes[0]], vertices[indexes[index]], vertices[indexes[index + 1]]))
    return Mesh(triangles)


def load_stl(path: Path) -> Mesh:
    data = path.read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            triangles = []
            offset = 84
            for _ in range(count):
                values = struct.unpack_from("<12f", data, offset)
                triangles.append(Triangle(Vertex(*values[3:6]), Vertex(*values[6:9]), Vertex(*values[9:12])))
                offset += 50
            return Mesh(triangles)
    text = data.decode("utf-8-sig", errors="replace")
    triangles: list[Triangle] = []
    current: list[Vertex] = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) >= 4 and parts[0].lower() == "vertex":
            current.append(_float_triplet(parts[1:4]))
            if len(current) == 3:
                triangles.append(Triangle(*current))
                current = []
    return Mesh(triangles)


def load_mesh(path: Path) -> Mesh:
    suffix = path.suffix.lower()
    if suffix == ".obj":
        mesh = load_obj(path)
    elif suffix == ".stl":
        mesh = load_stl(path)
    else:
        raise ValueError("目前支持 STL 或 OBJ 文件")
    if not mesh.triangles:
        raise ValueError("文件中没有有效三角面")
    return mesh


def rotate_mesh(mesh: Mesh, rotation_x: float, rotation_y: float, rotation_z: float) -> Mesh:
    """Rotate around the model center before bed fitting and slicing."""
    if not any(abs(value) > EPS for value in (rotation_x, rotation_y, rotation_z)):
        return mesh
    min_x, max_x, min_y, max_y, min_z, max_z = mesh.bounds
    center = ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
    rx, ry, rz = (math.radians(value) for value in (rotation_x, rotation_y, rotation_z))
    sin_x, cos_x = math.sin(rx), math.cos(rx)
    sin_y, cos_y = math.sin(ry), math.cos(ry)
    sin_z, cos_z = math.sin(rz), math.cos(rz)

    def point(vertex: Vertex) -> Vertex:
        x, y, z = vertex.x - center[0], vertex.y - center[1], vertex.z - center[2]
        y, z = y * cos_x - z * sin_x, y * sin_x + z * cos_x
        x, z = x * cos_y + z * sin_y, -x * sin_y + z * cos_y
        x, y = x * cos_z - y * sin_z, x * sin_z + y * cos_z
        return Vertex(x + center[0], y + center[1], z + center[2])

    return Mesh([Triangle(point(triangle.a), point(triangle.b), point(triangle.c)) for triangle in mesh.triangles])


def align_mesh_to_z(mesh: Mesh, direction: tuple[float, float, float]) -> Mesh:
    """Rotate a model so the requested slicing direction becomes world +Z."""
    length = math.sqrt(sum(value * value for value in direction))
    if length <= EPS:
        return mesh
    nx, ny, nz = (value / length for value in direction)
    dot = max(-1.0, min(1.0, nz))
    axis = (ny, -nx, 0.0)
    axis_length = math.hypot(axis[0], axis[1])
    if axis_length <= EPS:
        if dot > 0:
            return mesh
        axis, angle = (1.0, 0.0, 0.0), math.pi
    else:
        axis = tuple(value / axis_length for value in axis)
        angle = math.atan2(axis_length, dot)
    sin_angle, cos_angle = math.sin(angle), math.cos(angle)
    min_x, max_x, min_y, max_y, min_z, max_z = mesh.bounds
    center = ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)

    def point(vertex: Vertex) -> Vertex:
        vector = (vertex.x - center[0], vertex.y - center[1], vertex.z - center[2])
        cross = (axis[1] * vector[2] - axis[2] * vector[1], axis[2] * vector[0] - axis[0] * vector[2], axis[0] * vector[1] - axis[1] * vector[0])
        projection = axis[0] * vector[0] + axis[1] * vector[1] + axis[2] * vector[2]
        rotated = tuple(vector[index] * cos_angle + cross[index] * sin_angle + axis[index] * projection * (1 - cos_angle) for index in range(3))
        return Vertex(rotated[0] + center[0], rotated[1] + center[1], rotated[2] + center[2])

    return Mesh([Triangle(point(triangle.a), point(triangle.b), point(triangle.c)) for triangle in mesh.triangles])


def _triangle_segment(triangle: Triangle, z: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    points: list[tuple[float, float]] = []
    for first, second in ((triangle.a, triangle.b), (triangle.b, triangle.c), (triangle.c, triangle.a)):
        da, db = first.z - z, second.z - z
        if abs(da) <= EPS and abs(db) <= EPS:
            continue
        if (da < -EPS and db > EPS) or (da > EPS and db < -EPS):
            ratio = da / (da - db)
            points.append((first.x + (second.x - first.x) * ratio, first.y + (second.y - first.y) * ratio))
        elif abs(da) <= EPS:
            points.append((first.x, first.y))
        elif abs(db) <= EPS:
            points.append((second.x, second.y))
    unique: list[tuple[float, float]] = []
    for point in points:
        if not any(math.hypot(point[0] - other[0], point[1] - other[1]) <= 1e-6 for other in unique):
            unique.append(point)
    if len(unique) == 2 and math.hypot(unique[0][0] - unique[1][0], unique[0][1] - unique[1][1]) > 1e-6:
        return unique[0], unique[1]
    return None


def _join_segments(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join triangle intersections into closed/open polylines using a grid key."""
    remaining = segments[:]
    paths: list[list[tuple[float, float]]] = []

    def key(point: tuple[float, float]) -> tuple[int, int]:
        return round(point[0] * 100000), round(point[1] * 100000)

    while remaining:
        start, end = remaining.pop()
        path = [start, end]
        changed = True
        while changed:
            changed = False
            for index, (left, right) in enumerate(remaining):
                if key(left) == key(path[-1]):
                    path.append(right); remaining.pop(index); changed = True; break
                if key(right) == key(path[-1]):
                    path.append(left); remaining.pop(index); changed = True; break
                if key(left) == key(path[0]):
                    path.insert(0, right); remaining.pop(index); changed = True; break
                if key(right) == key(path[0]):
                    path.insert(0, left); remaining.pop(index); changed = True; break
        if len(path) >= 3:
            if key(path[0]) != key(path[-1]):
                path.append(path[0])
            paths.append(path)
    return paths


def _path_area(path: list[tuple[float, float]]) -> float:
    """鞋带公式计算闭合多边形面积。首尾相接（含闭合边），因此对去掉首尾闭合点的开放列表同样正确。"""
    if len(path) < 3:
        return 0.0
    return 0.5 * sum(
        path[i][0] * path[(i + 1) % len(path)][1] - path[(i + 1) % len(path)][0] * path[i][1]
        for i in range(len(path))
    )


def _ring_points(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """去掉重复顶点（含首尾闭合点），保证每条边都有非零长度。"""
    points: list[tuple[float, float]] = []
    for point in path:
        if not points or math.hypot(point[0] - points[-1][0], point[1] - points[-1][1]) > MERGE_EPS:
            points.append(point)
    while len(points) >= 2 and math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]) <= MERGE_EPS:
        points.pop()
    return points


def _remove_collinear(points: list[tuple[float, float]], eps: float = MERGE_EPS) -> list[tuple[float, float]]:
    """移除共线的冗余顶点，避免等距偏移在接近共线的短边上提前塌缩。

    切片产生的轮廓常在长边上夹着冗余共线点（比如矩形边上的中间顶点）。
    这些点不改变形状，却会让 _offset_path 的"边反向"检查在偏移量超过该
    短边长度时返回 None，导致回字形填充只走外半圈就停住、离孔还差一大截。
    """
    if len(points) < 3:
        return points
    cleaned: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        before = points[index - 1]
        after = points[(index + 1) % len(points)]
        cross = (point[0] - before[0]) * (after[1] - before[1]) - (point[1] - before[1]) * (after[0] - before[0])
        segment = math.hypot(after[0] - before[0], after[1] - before[1])
        if segment > EPS and abs(cross) <= eps * segment:
            continue  # 点在前后两点连线上，冗余
        cleaned.append(point)
    return cleaned


def _collapse_short_edges(points: list[tuple[float, float]], minimum: float) -> list[tuple[float, float]]:
    """合并比 minimum 还短的边，消除切片轮廓上的锯齿抖动。

    切面轮廓由三角面与切平面求交得到，相邻点常呈"长短交替"的微小抖动
    （实测出现 0.007mm 与 0.071mm 交替的边）。内缩时这些抖动边会先翻转，
    使 _offset_path 的"边不可反向"判据在第一次偏移就返回 None，表现为
    实心截面只描了一圈外轮廓、回字形填充完全出不来。

    这里反复合并当前最短的边（删除该边的后一个端点），直到所有边都不短于
    minimum。minimum 取一个环距：比环距还短的边小于填充分辨率本身，
    保留它们只会让内缩失败，不影响回字形能表达的几何。
    """
    pts = list(points)
    while len(pts) > 3:
        count = len(pts)
        shortest = min(
            range(count),
            key=lambda index: math.hypot(
                pts[(index + 1) % count][0] - pts[index][0],
                pts[(index + 1) % count][1] - pts[index][1],
            ),
        )
        length = math.hypot(
            pts[(shortest + 1) % count][0] - pts[shortest][0],
            pts[(shortest + 1) % count][1] - pts[shortest][1],
        )
        if length >= minimum:
            break
        pts.pop((shortest + 1) % count)
    return pts


def _offset_path(path: list[tuple[float, float]], distance: float) -> list[tuple[float, float]] | None:
    """向内等距偏移闭合多边形，返回均匀间距的内环（真偏移，非形心缩放）。

    偏移到形状塌缩（某条边反向或夹角退化为尖刺）时返回 None，
    由调用方停止继续生成内环，避免产出退化刀路。
    """
    if distance <= 0:
        return path[:]
    points = _collapse_short_edges(
        _remove_collinear(_ring_points(path)),
        max(MERGE_EPS, distance),
    )
    count = len(points)
    if count < 3:
        return None
    sign = 1.0 if _path_area(points) >= 0 else -1.0

    def inward(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= EPS:
            return 0.0, 0.0
        return (-dy / length, dx / length) if sign > 0 else (dy / length, -dx / length)

    offset: list[tuple[float, float]] = []
    for index in range(count):
        curr = points[index]
        nx, ny = inward(curr, points[(index + 1) % count])
        pnx, pny = inward(points[(index - 1) % count], curr)
        denom = 1.0 + (nx * pnx + ny * pny)
        if denom <= MERGE_EPS:
            return None
        offset.append((curr[0] + distance * (nx + pnx) / denom, curr[1] + distance * (ny + pny) / denom))
    for index in range(count):
        ax, ay = points[index]
        bx, by = points[(index + 1) % count]
        cx, cy = offset[index]
        dx, dy = offset[(index + 1) % count]
        if (bx - ax) * (dx - cx) + (by - ay) * (dy - cy) <= 0.0:
            return None
    return offset + [offset[0]]


def _point_in_path(point: tuple[float, float], path: list[tuple[float, float]]) -> bool:
    """射线法：判断点是否落在闭合折线内部。"""
    x, y = point
    inside = False
    for index in range(len(path) - 1):
        x0, y0 = path[index]
        x1, y1 = path[index + 1]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _point_on_path(point: tuple[float, float], path: list[tuple[float, float]]) -> bool:
    """点是否贴在某条边上（含端点），用于把"沿着孔壁走"和"走进孔里"区分开。"""
    x, y = point
    for index in range(len(path) - 1):
        x0, y0 = path[index]
        x1, y1 = path[index + 1]
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        if length_sq <= EPS:
            continue
        ratio = min(1.0, max(0.0, ((x - x0) * dx + (y - y0) * dy) / length_sq))
        if math.hypot(x - (x0 + dx * ratio), y - (y0 + dy * ratio)) <= MERGE_EPS:
            return True
    return False


def _in_material(point: tuple[float, float], paths: list[list[tuple[float, float]]]) -> bool:
    """奇偶填充规则：落在奇数条轮廓内部的位置才是实体材料，偶数层是孔洞空腔。

    这是同层多条轮廓之间唯一可靠的判据：孔的绕向、内岛被孔包住的嵌套关系都不影响
    结果，因此不依赖网格绕向是否一致。

    贴着轮廓边界的点按"不在该轮廓内"计。否则当孔壁恰好落在某条环上时（孔壁到外壁
    的距离是间距整数倍时非常常见），整条环会被误判为走进空腔而被砍掉，导致该层几乎
    没有填充。
    """
    count = sum(
        1 for path in paths
        if _point_in_path(point, path) and not _point_on_path(point, path)
    )
    return count % 2 == 1


def _loop_signature(path: list[tuple[float, float]]) -> tuple[int, float, float, float, float]:
    """顶点数与包围盒。作为重合判断的廉价预筛，避免对每条环都做完整归约比较。"""
    points = _remove_collinear(_ring_points(path))
    if not points:
        return 0, 0.0, 0.0, 0.0, 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return len(points), min(xs), min(ys), max(xs), max(ys)


def _same_signature(first: tuple, second: tuple) -> bool:
    if first[0] != second[0] or first[0] == 0:
        return False
    return all(abs(a - b) <= MERGE_EPS for a, b in zip(first[1:], second[1:]))


def _normalized_loop(path: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """去掉重复/闭合点与共线冗余点，再旋转到字典序最小顶点，便于比较两条轮廓是否重合。"""
    points = _remove_collinear(_ring_points(path))
    if not points:
        return ()
    start = min(range(len(points)), key=lambda index: points[index])
    return tuple(points[start:] + points[:start])


def _same_loop(first: tuple[tuple[float, float], ...], second: tuple[tuple[float, float], ...]) -> bool:
    if not first or len(first) != len(second):
        return False
    count = len(first)
    forward = all(
        math.hypot(a[0] - b[0], a[1] - b[1]) <= MERGE_EPS for a, b in zip(first, second)
    )
    if forward:
        return True
    # 孔与外轮廓偏移环的绕向可能相反，归一化到同一顶点后仍要按反向再比一次。
    # 反向遍历从同一最小顶点出发，因此索引映射是 (count - index) % count，而非直接倒序。
    return all(
        math.hypot(a[0] - second[(count - index) % count][0], a[1] - second[(count - index) % count][1]) <= MERGE_EPS
        for index, a in enumerate(first)
    )


def _concentric_rings(source_path: list[tuple[float, float]], settings: Settings) -> list[list[tuple[float, float]]]:
    """外轮廓 + 同心内环（回字形），环间距恒等于 settings.fill_spacing。

    每一条内环都由前一条内环再偏移 fill_spacing 得到（逐环递推），
    因此相邻环的垂直间距处处相等，不会随环数累积误差。
    """
    rings: list[list[tuple[float, float]]] = [source_path]
    if settings.fill_enabled and settings.fill_spacing > 0:
        current = source_path
        while len(rings) < MAX_FILL_RINGS:
            ring = _offset_path(current, settings.fill_spacing)
            if ring is None or len(ring) < 3 or abs(_path_area(ring)) <= 0.01:
                break
            rings.append(ring)
            current = ring
    elif settings.gap > 0:
        # 未启用回字形时仍走两圈供料环，但同样用等距偏移，
        # 保证 ring0→1→2 的间距恒等于 gap（旧版按形心缩放，长边/短边间距相差一倍）。
        current = source_path
        while len(rings) <= 2:
            ring = _offset_path(current, settings.gap)
            if ring is None or len(ring) < 3 or abs(_path_area(ring)) <= 0.01:
                break
            rings.append(ring)
            current = ring
    return rings


def _rotate_to_diagonal_start(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """把闭合环的起点转到"对角角点"，使环与环之间沿对角线切换。

    与项目既有回字路径（如 Prints/cube15 系列）一致：每圈都从同一个方向的
    角点起刀——方形的左下角 (5,5) → (6,6) → (7,7)，圈间用一段 G0 沿 45°
    对角线推进。这里取 x+y 最小的顶点作为起刀角点，方形轮廓即左下角，
    圆形或凹形轮廓则取其最靠近左下方向的顶点，效果一致。
    """
    points = _ring_points(path)
    if len(points) < 3:
        return path
    start = min(range(len(points)), key=lambda index: (points[index][0] + points[index][1], points[index][1]))
    rotated = points[start:] + points[:start]
    return rotated + [rotated[0]]


def slice_mesh(mesh: Mesh, settings: Settings) -> list[SliceLayer]:
    mesh = rotate_mesh(mesh, settings.rotation_x, settings.rotation_y, settings.rotation_z)
    mesh = align_mesh_to_z(mesh, settings.slice_direction)
    min_x, max_x, min_y, max_y, min_z, max_z = mesh.bounds
    span_x, span_y = max_x - min_x, max_y - min_y
    scale = 1.0
    usable_x, usable_y = settings.bed_x - 2 * settings.margin, settings.bed_y - 2 * settings.margin
    if usable_x <= EPS or usable_y <= EPS:
        raise ValueError("工作区尺寸必须大于两倍边距")
    if settings.auto_scale and max(span_x, span_y) > 0:
        scale = min(1.0, usable_x / max(span_x, EPS), usable_y / max(span_y, EPS))
    offset_x = (settings.bed_x - span_x * scale) / 2 - min_x * scale if settings.center_model else settings.margin - min_x * scale
    offset_y = (settings.bed_y - span_y * scale) / 2 - min_y * scale if settings.center_model else settings.margin - min_y * scale
    triangles = [
        Triangle(
            Vertex(t.a.x * scale + offset_x, t.a.y * scale + offset_y, t.a.z),
            Vertex(t.b.x * scale + offset_x, t.b.y * scale + offset_y, t.b.z),
            Vertex(t.c.x * scale + offset_x, t.c.y * scale + offset_y, t.c.z),
        ) for t in mesh.triangles
    ]
    height = max_z - min_z
    first = max(0.01, settings.first_layer_height)
    normal = max(0.01, settings.layer_height)
    layers = max(1, math.ceil(max(0.0, height - first) / normal) + 1)
    result: list[SliceLayer] = []
    for layer_no in range(1, layers + 1):
        z = min_z + first if layer_no == 1 else min_z + first + (layer_no - 1) * normal
        z = min(z, max_z - EPS if max_z - min_z > EPS else max_z + EPS)
        segments = [segment for triangle in triangles if (segment := _triangle_segment(triangle, z)) is not None]
        paths = _join_segments(segments)
        paths = [path for path in paths if abs(_path_area(path)) > 1e-5]
        material = "A" if layer_no <= settings.a_layers else "B"
        result.append(SliceLayer(layer_no, z - min_z, material, paths))
    return result


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_grayscale_png(path: Path, pixels: bytearray, width: int, height: int) -> None:
    """Write an 8-bit grayscale PNG without requiring Pillow at runtime."""
    if len(pixels) != width * height:
        raise ValueError("掩码像素缓冲区尺寸不匹配")
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    compressor = zlib.compressobj(level=9)
    with path.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(_png_chunk(b"IHDR", header))
        for row in range(height):
            start = row * width
            data = compressor.compress(b"\0" + pixels[start:start + width])
            if data:
                handle.write(_png_chunk(b"IDAT", data))
        data = compressor.flush()
        if data:
            handle.write(_png_chunk(b"IDAT", data))
        handle.write(_png_chunk(b"IEND", b""))


def _rasterize_layer(layer: SliceLayer, settings: Settings) -> bytearray:
    """Fill sliced contours using an even-odd scanline rule in projector pixels."""
    pixels = bytearray(MASK_WIDTH * MASK_HEIGHT)
    if not layer.paths:
        return pixels
    if settings.bed_x <= EPS or settings.bed_y <= EPS:
        raise ValueError("工作区尺寸必须大于 0")

    scale_x = MASK_WIDTH / settings.bed_x
    scale_y = MASK_HEIGHT / settings.bed_y
    intersections: list[list[float]] = [[] for _ in range(MASK_HEIGHT)]
    for path in layer.paths:
        if len(path) < 3:
            continue
        points = [(x * scale_x, (settings.bed_y - y) * scale_y) for x, y in path]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(y1 - y0) <= EPS:
                continue
            first_row = max(0, math.ceil(min(y0, y1)))
            last_row = min(MASK_HEIGHT - 1, math.floor(max(y0, y1)))
            for row in range(first_row, last_row + 1):
                y = float(row)
                if min(y0, y1) <= y <= max(y0, y1):
                    ratio = (y - y0) / (y1 - y0)
                    intersections[row].append(x0 + (x1 - x0) * ratio)

    for row, crossings in enumerate(intersections):
        crossings.sort()
        for left, right in zip(crossings[::2], crossings[1::2]):
            start = max(0, math.ceil(left))
            end = min(MASK_WIDTH - 1, math.floor(right))
            if end >= start:
                offset = row * MASK_WIDTH
                pixels[offset + start:offset + end + 1] = b"\xff" * (end - start + 1)
    return pixels


def write_mask_images(layers: list[SliceLayer], settings: Settings, output_directory: Path) -> None:
    for layer in layers:
        _write_grayscale_png(output_directory / f"{layer.number}.png", _rasterize_layer(layer, settings), MASK_WIDTH, MASK_HEIGHT)


def _estimated_print_seconds(layers: list[SliceLayer], settings: Settings) -> int:
    lift_seconds = 5.0 / max(settings.feed_speed, EPS) * 60.0
    drop_seconds = 5.0 / max(settings.travel_speed, EPS) * 60.0
    exposure_seconds = sum(settings.support_dwell if layer.number <= settings.support_layers else settings.exposure for layer in layers)
    xy_seconds = sum(
        math.dist(start, end) / max(settings.xy_feed_speed, EPS) * 60.0
        for layer in layers
        for path in layer.paths
        for start, end in zip(path, path[1:])
    )
    return max(0, round(len(layers) * (lift_seconds + drop_seconds) + exposure_seconds + xy_seconds))


def build_gcode(layers: list[SliceLayer], settings: Settings, output_directory: Path, model_name: str) -> str:
    """Build DLP controller G-code that loads numbered masks from its own directory."""
    lines = [
        f";fileName:{output_directory} - {settings.bed_x:g}x{settings.bed_y:g}.zip",
        f";modelName:{model_name}",
        ";machineType:default",
        f";estimatedPrintTime:{_estimated_print_seconds(layers, settings)}",
        f";layerHeight:{settings.layer_height:.3f}",
        f";resolutionX:{MASK_WIDTH}",
        f";resolutionY:{MASK_HEIGHT}",
        f";machineX:{settings.bed_x:g}",
        f";machineY:{settings.bed_y:g}",
        f";machineZ:{MACHINE_Z:g}",
        ";projectType:DLP_normal",
        f";normalExposureTime:{settings.exposure:g}",
        f";bottomLayExposureTime:{settings.support_dwell:g}",
        f";bottomLayerExposureTime:{settings.support_dwell:g}",
        f";normalDropSpeed:{settings.travel_speed:g}",
        ";normalLayerLiftHeight:5",
        f";normalLayerLiftSpeed:{settings.feed_speed:g}",
        f";pathSpacing:{settings.gap:g}",
        f";xyFeedSpeed:{settings.xy_feed_speed:g}",
        f";xyTravelSpeed:{settings.xy_travel_speed:g}",
        f";bottomLayCount:{settings.support_layers}",
        f";bottomLayerCount:{settings.support_layers}",
        ";mirror:0",
        f";totalLayer:{len(layers)}",
        ";START_GCODE_BEGIN",
        "G21;",
        "G90;",
        "M106 S0;",
        "G28 Z0;",
        ";START_GCODE_END",
    ]
    for layer in layers:
        exposure = settings.support_dwell if layer.number <= settings.support_layers else settings.exposure
        lines += [
            "",
            f";LAYER_START:{layer.number - 1}",
            f";currPos:{layer.z:.3f}",
            f'M6054 "{layer.number}.png";show Image',
            f"G0 Z{min(MACHINE_Z, layer.z + 5.0):.6f} F{settings.feed_speed:.6f};",
            f"G0 Z{layer.z:.6f} F{settings.travel_speed:.6f};",
            "G4 P0;",
        ]
        siblings = layer.paths if len(layer.paths) > 1 else None
        sibling_signatures = (
            [_loop_signature(other) for other in layer.paths] if siblings is not None else []
        )
        sibling_loops: list[tuple | None] = [None] * len(layer.paths)
        for source_index, source_path in enumerate(layer.paths):
            rings = _concentric_rings(source_path, settings)
            usable_rings: list[list[tuple[float, float]]] = []
            for path in rings:
                if len(path) < 2:
                    continue
                # 带孔洞的截面：环一旦走出实体材料（进入同层孔洞或越出外壁）就停止，别在空腔里走料。
                # 判据必须是"点是否落在材料内"（奇偶规则），而不是"点是否落在别的轮廓内"：
                # 后者会让被孔洞包住的独立内岛整条轮廓都落在其它轮廓内部，于是一圈都走不出来。
                if siblings is not None and path is not source_path:
                    if any(not _in_material(point, siblings) for point in path[:-1]):
                        break
                    # 环正好落在同层另一条轮廓上（孔壁与环重合）时跳过，避免重复走一遍。
                    signature = _loop_signature(path)
                    duplicate = False
                    for index in range(len(siblings)):
                        if index == source_index or not _same_signature(signature, sibling_signatures[index]):
                            continue
                        if sibling_loops[index] is None:
                            sibling_loops[index] = _normalized_loop(siblings[index])
                        if _same_loop(_normalized_loop(path), sibling_loops[index]):
                            duplicate = True
                            break
                    if duplicate:
                        continue
                usable_rings.append(path)
            # 由内向外：先走最内圈，再一圈圈往外扩；每圈都从对角角点起刀，
            # 圈间用一段 G0 沿对角线切到下一圈（与既有 run.gcode 的回字路径一致）。
            for path in reversed(usable_rings):
                path = _rotate_to_diagonal_start(path)
                first_x, first_y = path[0]
                lines += [
                    f"G0 X{first_x:.3f} Y{first_y:.3f} F{settings.xy_travel_speed:.6f};",
                    "M101;",
                ]
                lines.extend(
                    f"G1 X{x:.3f} Y{y:.3f} F{settings.xy_feed_speed:.6f};"
                    for x, y in path[1:]
                )
                lines.append("M102;")
        lines += [
            "M106 S255.000000;light on",
            f"G4 P{int(round(exposure * 1000))};",
            "M106 S0; light off",
            "",
            ";LAYER_END",
    ]
    lines += [";END_GCODE_BEGIN", "M106 S0;", f"G1 Z{MACHINE_Z:.6f} F25;", "M18;", ";END_GCODE_END"]
    return "\n".join(lines) + "\n"


def write_gcode(layers: list[SliceLayer], settings: Settings, output: Path, model_name: str) -> None:
    output.write_text(build_gcode(layers, settings, output.parent, model_name), encoding="utf-8")


def write_print_job(layers: list[SliceLayer], settings: Settings, output_directory: Path, model_name: str) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    write_mask_images(layers, settings, output_directory)
    output = output_directory / "run.gcode"
    write_gcode(layers, settings, output, model_name)
    return output


class ToolpathPreview:
    """Embedded OpenGL inspection of the exact hybrid G-code about to be exported."""

    FRAME_INTERVAL_MS = 16

    def __init__(self, parent: tk.Misc, model: object) -> None:
        from gcode_viewer_3d import OpenGLViewport

        self.model = model
        self.playing = False
        self._alive = True
        self._preview_ready = False
        self.last_tick = time.perf_counter()
        self.window = tk.Toplevel(parent)
        self.window.title("混合刀路预览")
        self.window.geometry("1380x880")
        self.window.minsize(1060, 700)
        self.window.configure(bg=SlicerApp.BG)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.layer_var = tk.IntVar(value=1)
        self.timeline_var = tk.DoubleVar(value=0.0)
        self.speed_var = tk.StringVar(value="10")
        self.show_travel = tk.BooleanVar(value=True)
        self.show_grid = tk.BooleanVar(value=True)
        self.show_machine = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="待预览")
        self.progress_var = tk.StringVar(value="00:00 / 00:00")
        self.position_var = tk.StringVar(value="X 0.000  Y 0.000  Z 0.000")
        self.layer_info_var = tk.StringVar(value="")

        header = ttk.Frame(self.window, padding=(20, 14))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="混合刀路预览", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=getattr(model, "name", "run.gcode"), style="Muted.TLabel").pack(side="left", padx=14)
        ttk.Button(header, text="关闭", command=self.close).pack(side="right")

        self.window.rowconfigure(1, weight=1)
        self.window.columnconfigure(1, weight=1)
        sidebar = ttk.Frame(self.window, style="Panel.TFrame", padding=12, width=230)
        sidebar.grid(row=1, column=0, sticky="nsw", padx=(16, 9), pady=(0, 12))
        sidebar.grid_propagate(False)
        ttk.Label(sidebar, text="图层", font=("Segoe UI Semibold", 14)).pack(anchor="w")
        ttk.Label(sidebar, textvariable=self.layer_info_var, style="Muted.TLabel", wraplength=195).pack(anchor="w", pady=(2, 8))
        layer_count = max(1, len(getattr(model, "layers", [])))
        self.layer_scale = tk.Scale(
            sidebar, from_=1, to=layer_count, orient="vertical", variable=self.layer_var,
            command=self.set_layer, showvalue=False, bg=SlicerApp.PANEL, fg=SlicerApp.TEXT,
            troughcolor="#263945", activebackground=SlicerApp.GREEN, highlightthickness=0,
            bd=0, sliderlength=34, width=14,
        )
        self.layer_scale.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        buttons = ttk.Frame(sidebar, style="Panel.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="俯视", command=lambda: self.viewport.set_preset("top")).pack(side="left", fill="x", expand=True, padx=(0, 3))
        ttk.Button(buttons, text="等轴", command=lambda: self.viewport.set_preset("iso")).pack(side="left", fill="x", expand=True, padx=3)
        ttk.Button(buttons, text="侧视", command=lambda: self.viewport.set_preset("side")).pack(side="left", fill="x", expand=True, padx=(3, 0))
        ttk.Button(sidebar, text="适应窗口", command=lambda: self.viewport.fit_view()).pack(fill="x", pady=(8, 6))
        for label, variable in (("显示空移", self.show_travel), ("显示网格", self.show_grid), ("显示机头", self.show_machine)):
            ttk.Checkbutton(sidebar, text=label, variable=variable, command=self.apply_display).pack(anchor="w", pady=2)

        viewport_holder = tk.Frame(self.window, bg="#090e13", highlightbackground=SlicerApp.BORDER, highlightthickness=1)
        viewport_holder.grid(row=1, column=1, sticky="nsew", padx=(0, 16), pady=(0, 12))
        viewport_holder.rowconfigure(0, weight=1)
        viewport_holder.columnconfigure(0, weight=1)
        self.viewport = OpenGLViewport(viewport_holder)
        self.viewport.grid(row=0, column=0, sticky="nsew")
        self.viewport.set_model(model)

        controls = tk.Frame(self.window, bg=SlicerApp.PANEL, highlightbackground=SlicerApp.BORDER, highlightthickness=1, padx=14, pady=10)
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 16))
        controls.columnconfigure(5, weight=1)
        tk.Label(controls, textvariable=self.status_var, bg=SlicerApp.PANEL, fg=SlicerApp.GREEN, font=("Segoe UI Semibold", 12)).grid(row=0, column=0, sticky="w")
        tk.Label(controls, textvariable=self.position_var, bg=SlicerApp.PANEL, fg=SlicerApp.TEXT, font=("Consolas", 11)).grid(row=0, column=1, columnspan=3, sticky="w", padx=18)
        tk.Label(controls, textvariable=self.progress_var, bg=SlicerApp.PANEL, fg=SlicerApp.MUTED, font=("Consolas", 11)).grid(row=0, column=5, sticky="e")
        ttk.Button(controls, text="复位", command=self.reset).grid(row=1, column=0, pady=(8, 0), padx=(0, 5))
        ttk.Button(controls, text="上一步", command=lambda: self.step(-1)).grid(row=1, column=1, pady=(8, 0), padx=3)
        self.play_button = ttk.Button(controls, text="播放", style="Accent.TButton", command=self.toggle_play)
        self.play_button.grid(row=1, column=2, pady=(8, 0), padx=3)
        ttk.Button(controls, text="下一步", command=lambda: self.step(1)).grid(row=1, column=3, pady=(8, 0), padx=3)
        self.timeline = tk.Scale(
            controls, from_=0, to=10000, orient="horizontal", variable=self.timeline_var,
            command=self.seek, showvalue=False, bg=SlicerApp.PANEL, fg=SlicerApp.TEXT,
            troughcolor="#263945", activebackground=SlicerApp.GREEN, highlightthickness=0,
            bd=0, sliderlength=30, width=14,
        )
        self.timeline.grid(row=1, column=4, columnspan=2, sticky="ew", padx=(10, 12), pady=(8, 0))
        ttk.Label(controls, text="速度", background=SlicerApp.PANEL, foreground=SlicerApp.MUTED).grid(row=1, column=6, pady=(8, 0), padx=(0, 5))
        speed = ttk.Combobox(controls, width=5, textvariable=self.speed_var, values=("1", "5", "10", "25", "50", "100"), state="readonly")
        speed.grid(row=1, column=7, pady=(8, 0))
        speed.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        self._preview_ready = True
        self.set_layer("1")
        self.window.after(self.FRAME_INTERVAL_MS, self.tick)

    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def layer_bounds(self) -> tuple[float, float, int, int]:
        moves = getattr(self.model, "moves", [])
        layers = getattr(self.model, "layers", [])
        if not moves:
            return 0.0, 0.0, 0, 0
        if not layers:
            return 0.0, moves[-1].time_end, 0, len(moves) - 1
        index = max(1, min(len(layers), self.layer_var.get())) - 1
        layer = layers[index]
        return moves[layer.first_move].time_start, moves[layer.last_move].time_end, layer.first_move, layer.last_move

    def set_layer(self, value: str) -> None:
        if not self._preview_ready:
            return
        layers = getattr(self.model, "layers", [])
        if not layers:
            return
        layer_number = max(1, min(len(layers), int(round(float(value)))))
        self.playing = False
        self.play_button.configure(text="播放")
        self.layer_var.set(layer_number)
        self.viewport.selected_layer = layer_number
        start, _end, _first, _last = self.layer_bounds()
        self.viewport.progress_time = start
        layer = layers[layer_number - 1]
        height = getattr(self.model, "moves")[layer.last_move].end.z
        self.layer_info_var.set(f"第 {layer_number} / {len(layers)} 层\n{layer.name} · Z {height:.3f} mm")
        self.refresh()
        self.viewport.render()

    def apply_display(self) -> None:
        self.viewport.show_travel = self.show_travel.get()
        self.viewport.show_grid = self.show_grid.get()
        self.viewport.show_machine = self.show_machine.get()
        self.viewport.invalidate_path_cache()
        self.viewport.render()

    def seek(self, value: str) -> None:
        if not self._alive:
            return
        self.playing = False
        self.play_button.configure(text="播放")
        start, end, _first, _last = self.layer_bounds()
        self.viewport.progress_time = start + (end - start) * float(value) / 10000.0
        self.refresh()
        self.viewport.render()

    def reset(self) -> None:
        self.playing = False
        self.play_button.configure(text="播放")
        start, _end, _first, _last = self.layer_bounds()
        self.viewport.progress_time = start
        self.refresh()
        self.viewport.render()

    def toggle_play(self) -> None:
        start, end, _first, _last = self.layer_bounds()
        if self.viewport.progress_time >= end:
            self.viewport.progress_time = start
        self.playing = not self.playing
        self.last_tick = time.perf_counter()
        self.play_button.configure(text="暂停" if self.playing else "播放")
        self.refresh()

    def step(self, direction: int) -> None:
        moves = getattr(self.model, "moves", [])
        if not moves:
            return
        self.playing = False
        self.play_button.configure(text="播放")
        current, _move, _point = self.viewport._current_move()
        _start, _end, first, last = self.layer_bounds()
        index = max(first, min(last, current + direction))
        self.viewport.progress_time = moves[index].time_start if direction < 0 else moves[index].time_end
        self.refresh()
        self.viewport.render()

    def refresh(self) -> None:
        start, end, _first, _last = self.layer_bounds()
        _index, move, point = self.viewport._current_move()
        progress = max(0.0, min(1.0, (self.viewport.progress_time - start) / max(end - start, EPS)))
        self.timeline_var.set(progress * 10000.0)
        self.progress_var.set(f"{self.format_time(self.viewport.progress_time - start)} / {self.format_time(end - start)}")
        self.position_var.set(f"X {point.x:.3f}  Y {point.y:.3f}  Z {point.z:.3f}  F {move.feed:.0f}" if move else "X 0.000  Y 0.000  Z 0.000")
        self.status_var.set("播放中" if self.playing else "已暂停")

    def tick(self) -> None:
        if not self._alive:
            return
        now = time.perf_counter()
        if self.playing:
            start, end, _first, _last = self.layer_bounds()
            try:
                speed = float(self.speed_var.get())
            except ValueError:
                speed = 1.0
            self.viewport.progress_time = min(end, max(start, self.viewport.progress_time + (now - self.last_tick) * speed))
            if self.viewport.progress_time >= end:
                self.playing = False
                self.play_button.configure(text="播放")
            self.refresh()
            self.viewport.render()
        self.last_tick = now
        try:
            self.window.after(self.FRAME_INTERVAL_MS, self.tick)
        except tk.TclError:
            self._alive = False

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self.playing = False
        self.window.destroy()


class SlicerApp:
    PREVIEW_TRIANGLE_LIMIT = 1200
    BG = "#0b1016"
    PANEL = "#121a23"
    PANEL_2 = "#17222d"
    BORDER = "#293845"
    TEXT = "#eef4f8"
    MUTED = "#93a6b2"
    CYAN = "#61c7ff"
    GREEN = "#51e39a"
    AMBER = "#ffbd66"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("VECTOR SLICE · HKUprint G-code Slicer")
        self.root.geometry("1500x1000")
        self.root.minsize(1180, 760)
        self.root.configure(bg=self.BG)
        self.mesh: Mesh | None = None
        self.layers: list[SliceLayer] = []
        self._settings: Settings | None = None
        self.path_var = tk.StringVar(value="未选择 STL / OBJ")
        self.output_var = tk.StringVar(value="未生成 G-code")
        self.output_directory: Path | None = None
        self.output_directory_var = tk.StringVar(value="作业根目录：请先选择")
        self.status_var = tk.StringVar(value="选择 STL 或 OBJ 后开始切片")
        self.layer_var = tk.IntVar(value=1)
        self.preview_mode = tk.StringVar(value="3d")
        self.view_yaw = -35.0
        self.view_pitch = 24.0
        self.view_zoom = 1.0
        self._drag_origin: tuple[int, int] | None = None
        self._drag_start: tuple[int, int] | None = None
        self._face_pick_candidates: list[tuple[tuple[tuple[float, float], tuple[float, float], tuple[float, float]], tuple[float, float, float], float]] = []
        self._selected_face_normal: tuple[float, float, float] | None = None
        self.slice_direction: tuple[float, float, float] | None = None
        self._preview_mesh_cache: Mesh | None = None
        self._preview_mesh_cache_key: tuple[int, float, float, float] | None = None
        self._preview_bounds: tuple[float, float, float, float, float, float] | None = None
        self.entries: dict[str, tk.StringVar] = {}
        self.auto_scale = tk.BooleanVar(value=True)
        self.center_model = tk.BooleanVar(value=True)
        self.fill_enabled = tk.BooleanVar(value=True)
        self._build_style()
        self._build_ui()

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 12))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 21))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 11))
        style.configure("TButton", font=("Segoe UI Semibold", 12), padding=(12, 8))
        style.configure("Accent.TButton", background="#176a62", foreground="#ffffff")
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 11))

    def _build_ui(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)
        ttk.Label(self.root, text="VECTOR SLICE", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", padx=24, pady=(18, 4))
        ttk.Label(self.root, text="按最近 DLP A/B 供料与层间曝光规则生成可预览的轮廓 G-code", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=24, pady=(20, 4))
        settings = self._scrollable_settings()
        preview = ttk.Frame(self.root, style="Panel.TFrame", padding=12)
        preview.grid(row=1, column=1, sticky="nsew", padx=(0, 18), pady=(8, 18))
        preview.rowconfigure(1, weight=1); preview.columnconfigure(0, weight=1)
        self._settings_ui(settings)
        settings.update_idletasks()
        self.settings_canvas.configure(width=settings.winfo_reqwidth())
        preview_toolbar = ttk.Frame(preview, style="Panel.TFrame")
        preview_toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(3, 8))
        preview_toolbar.columnconfigure(3, weight=1)
        ttk.Label(preview_toolbar, text="预览", font=("Segoe UI Semibold", 16)).grid(row=0, column=0, sticky="w", padx=(0, 14))
        ttk.Button(preview_toolbar, text="3D 模型", command=lambda: self.set_preview_mode("3d")).grid(row=0, column=1, padx=3)
        ttk.Button(preview_toolbar, text="当前层", command=lambda: self.set_preview_mode("layer")).grid(row=0, column=2, padx=3)
        ttk.Button(preview_toolbar, text="正对选中面", command=self.face_on).grid(row=0, column=3, padx=3)
        ttk.Button(preview_toolbar, text="重置视角", command=self.reset_view).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(preview_toolbar, text="当前视角作为 Z", command=self.lock_slice_direction).grid(row=0, column=5, padx=(3, 0))
        self.canvas = tk.Canvas(preview, bg="#080e14", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        controls = ttk.Frame(preview, style="Panel.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="层", style="Muted.TLabel").grid(row=0, column=0, padx=(8, 4))
        self.layer_scale = tk.Scale(controls, from_=1, to=1, orient="horizontal", variable=self.layer_var, command=lambda _v: self.draw_preview(), showvalue=False, bg=self.PANEL, fg=self.TEXT, troughcolor="#2a414b", activebackground=self.GREEN, highlightthickness=0)
        self.layer_scale.grid(row=0, column=1, sticky="ew")
        self.layer_label = ttk.Label(controls, text="0 / 0", style="Muted.TLabel")
        self.layer_label.grid(row=0, column=2, padx=8)
        ttk.Label(self.root, textvariable=self.status_var, background=self.BG, foreground=self.CYAN, font=("Segoe UI", 11)).grid(row=2, column=0, columnspan=2, sticky="w", padx=24, pady=(0, 14))
        self.canvas.bind("<Configure>", lambda _event: self.draw_preview())
        self.canvas.bind("<ButtonPress-1>", self.begin_view_drag)
        self.canvas.bind("<B1-Motion>", self.drag_view)
        self.canvas.bind("<ButtonRelease-1>", self.pick_face)
        self.canvas.bind("<MouseWheel>", self.zoom_view)
        self.canvas.bind("<Button-4>", lambda _event: self.zoom_view_delta(1.1))
        self.canvas.bind("<Button-5>", lambda _event: self.zoom_view_delta(1 / 1.1))

    def _scrollable_settings(self) -> ttk.Frame:
        """左侧参数面板放进可滚动容器，返回真正承载控件的内层 Frame。

        参数区内容约 1340px 高，而窗口高度只有 1000px（最大化也才 ~1020px），
        直接 grid 的话「模型旋转」「回字形填充」等底部参数会被窗口切掉、点不到。
        """
        container = ttk.Frame(self.root, style="Panel.TFrame")
        container.grid(row=1, column=0, sticky="nsew", padx=(18, 10), pady=(8, 18))
        container.rowconfigure(0, weight=1); container.columnconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(container, bg=self.PANEL, highlightthickness=0)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.settings_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        settings = ttk.Frame(self.settings_canvas, style="Panel.TFrame", padding=16)
        window = self.settings_canvas.create_window((0, 0), window=settings, anchor="nw")
        settings.bind("<Configure>", lambda _event: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda event: self.settings_canvas.itemconfigure(window, width=event.width))
        self.root.bind_all("<MouseWheel>", self._scroll_settings)
        return settings

    def _scroll_settings(self, event) -> None:
        """滚轮滚动参数面板；指针不在面板内时放行，别抢 3D 预览的缩放。"""
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.settings_canvas:
                self.settings_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            widget = widget.master

    def _settings_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="模型与输出", font=("Segoe UI Semibold", 16)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Button(parent, text="打开 STL / OBJ", command=self.open_model).grid(row=1, column=0, columnspan=3, sticky="ew", pady=3)
        ttk.Label(parent, textvariable=self.path_var, style="Muted.TLabel", wraplength=330).grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 12))
        ttk.Button(parent, text="选择作业根目录", command=self.choose_output_directory).grid(row=3, column=0, columnspan=3, sticky="ew", pady=3)
        ttk.Label(parent, textvariable=self.output_directory_var, style="Muted.TLabel", wraplength=330).grid(row=4, column=0, columnspan=3, sticky="w", pady=(3, 10))
        ttk.Label(parent, text=f"混合模式：XY 走刀 + run.gcode + 1.png 至 N.png（{MASK_WIDTH} × {MASK_HEIGHT} 灰度掩码）", style="Muted.TLabel", wraplength=330).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 7))
        ttk.Button(parent, text="更新 3D 预览", command=self.refresh_model_preview).grid(row=6, column=0, columnspan=3, sticky="ew", pady=3)
        ttk.Button(parent, text="开始切片", style="Accent.TButton", command=self.slice).grid(row=7, column=0, columnspan=3, sticky="ew", pady=3)
        job_actions = ttk.Frame(parent, style="Panel.TFrame")
        job_actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=3)
        job_actions.columnconfigure((0, 1), weight=1)
        ttk.Button(job_actions, text="预览混合刀路", command=self.preview_toolpath).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(job_actions, text="导出打印作业", command=self.export).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        ttk.Label(parent, textvariable=self.output_var, style="Muted.TLabel", wraplength=330).grid(row=9, column=0, columnspan=3, sticky="w", pady=(3, 14))
        ttk.Separator(parent).grid(row=10, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(parent, text="切片参数", font=("Segoe UI Semibold", 16)).grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 8))
        fields = (
            ("layer_height", "普通层高 (mm)", "0.05"), ("first_layer_height", "首层层高 (mm)", "0.05"),
            ("feed_speed", "抬升速度 (mm/min)", "65"), ("travel_speed", "下降速度 (mm/min)", "150"),
            ("xy_feed_speed", "XY 走刀速度 (mm/min)", "180"), ("xy_travel_speed", "XY 空移速度 (mm/min)", "1200"),
            ("gap", "供料间距 (mm)", "1.0"), ("fill_spacing", "填充间距 (mm)", "1.0"),
            ("support_layers", "底层数量", "8"), ("support_dwell", "底层曝光 (s)", "50"),
            ("exposure", "普通层曝光 (s)", "6"), ("bed_x", "工作区 X (mm)", "80"),
            ("bed_y", "工作区 Y (mm)", "45"), ("margin", "边距 (mm)", "3"),
        )
        fields += (
            ("rotation_x", "模型旋转 X (°)", "0"),
            ("rotation_y", "模型旋转 Y (°)", "0"),
            ("rotation_z", "模型旋转 Z (°)", "0"),
        )
        for row, (key, label, default) in enumerate(fields, 12):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=default); self.entries[key] = var
            ttk.Entry(parent, textvariable=var, width=11).grid(row=row, column=1, columnspan=2, sticky="e", pady=3)
        ttk.Label(parent, text="旋转角度会应用到切片；鼠标拖动只改变 3D 观察视角").grid(row=29, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Label(parent, text="每层顺序：Z 定位 → XY 轮廓与回字形填充 → 掩码曝光").grid(row=30, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Label(parent, text="前 N 层使用底层曝光；其余层使用普通层曝光").grid(row=31, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Checkbutton(parent, text="自动缩放到工作区", variable=self.auto_scale).grid(row=32, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(parent, text="模型居中放置", variable=self.center_model).grid(row=33, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(parent, text="回字形填充（强制开启）", variable=self.fill_enabled, state="disabled").grid(row=34, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(parent, text="掩码坐标：G-code 原点在工作区左下，投影画面自动翻转 Y 轴", style="Muted.TLabel", wraplength=330).grid(row=35, column=0, columnspan=3, sticky="w", pady=(14, 0))

    def open_model(self) -> None:
        filename = filedialog.askopenfilename(filetypes=(("3D 模型", "*.stl *.obj"), ("STL", "*.stl"), ("OBJ", "*.obj")))
        if not filename:
            return
        try:
            self.mesh = load_mesh(Path(filename))
            self.slice_direction = None
            self.layers = []
            self._settings = None
            self._preview_mesh_cache = None
            self._preview_bounds = None
            self.output_var.set("未生成 G-code")
            self.layer_scale.configure(to=1)
            self.layer_var.set(1)
            self.path_var.set(Path(filename).name)
            bounds = self.mesh.bounds
            self.status_var.set(f"已载入 {len(self.mesh.triangles):,} 个三角面 · 尺寸 {bounds[1]-bounds[0]:.2f} × {bounds[3]-bounds[2]:.2f} × {bounds[5]-bounds[4]:.2f} mm")
            self.set_preview_mode("3d")
            self.reset_view()
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法读取模型", str(exc), parent=self.root)

    def choose_output_directory(self) -> None:
        initialdir = str(self.output_directory) if self.output_directory and self.output_directory.is_dir() else str(Path.cwd())
        directory = filedialog.askdirectory(title="选择打印作业根目录", initialdir=initialdir, parent=self.root)
        if not directory:
            return
        self.output_directory = Path(directory)
        self.output_directory_var.set(f"作业根目录：{self.output_directory}")
        self.status_var.set("已设置打印作业根目录")

    def set_preview_mode(self, mode: str) -> None:
        self.preview_mode.set(mode)
        self.draw_preview()

    def reset_view(self) -> None:
        self.view_yaw, self.view_pitch, self.view_zoom = -35.0, 24.0, 1.0
        self._selected_face_normal = None
        self.draw_preview()

    def begin_view_drag(self, event: tk.Event) -> None:
        if self.preview_mode.get() == "3d":
            self._drag_origin = (event.x, event.y)
            self._drag_start = (event.x, event.y)

    def drag_view(self, event: tk.Event) -> None:
        if self.preview_mode.get() != "3d" or self._drag_origin is None:
            return
        last_x, last_y = self._drag_origin
        self.view_yaw += (event.x - last_x) * 0.65
        self.view_pitch = max(-89.0, min(89.0, self.view_pitch + (event.y - last_y) * 0.65))
        self._drag_origin = (event.x, event.y)
        self.draw_preview()

    def pick_face(self, event: tk.Event) -> None:
        start = self._drag_start
        self._drag_origin = None
        self._drag_start = None
        if self.preview_mode.get() != "3d" or start is None:
            return
        if math.hypot(event.x - start[0], event.y - start[1]) > 6:
            return
        for triangle, normal, _depth in sorted(self._face_pick_candidates, key=lambda item: item[2], reverse=True):
            if self.point_in_triangle((event.x, event.y), triangle):
                self._selected_face_normal = normal
                self.face_on()
                return

    @staticmethod
    def point_in_triangle(point: tuple[float, float], triangle: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]) -> bool:
        (px, py), (ax, ay), (bx, by), (cx, cy) = point, *triangle
        first = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        second = (cx - bx) * (py - by) - (cy - by) * (px - bx)
        third = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
        return (first >= -1e-6 and second >= -1e-6 and third >= -1e-6) or (first <= 1e-6 and second <= 1e-6 and third <= 1e-6)

    def face_on(self) -> None:
        if self.preview_mode.get() != "3d" or self._selected_face_normal is None:
            self.status_var.set("请先在 3D 模型上单击一个面")
            return
        nx, ny, nz = self._selected_face_normal
        horizontal = math.hypot(nx, ny)
        self.view_yaw = math.degrees(math.atan2(nx, ny)) if horizontal > EPS else 0.0
        self.view_pitch = math.degrees(math.atan2(horizontal, nz))
        self.status_var.set("已正对选中的模型面；拖动可继续微调视角")
        self.draw_preview()

    def current_view_direction(self) -> tuple[float, float, float]:
        yaw, pitch = math.radians(self.view_yaw), math.radians(self.view_pitch)
        direction = (math.sin(yaw) * math.sin(pitch), math.cos(yaw) * math.sin(pitch), math.cos(pitch))
        length = math.sqrt(sum(value * value for value in direction))
        return tuple(value / max(length, EPS) for value in direction)

    def lock_slice_direction(self) -> None:
        if not self.mesh:
            self.status_var.set("请先打开模型，再设置当前视角为 Z 切片方向")
            return
        self.slice_direction = self.current_view_direction()
        direction = self.slice_direction
        self.status_var.set(f"已锁定 Z 切片方向：({direction[0]:.2f}, {direction[1]:.2f}, {direction[2]:.2f})")
        self.draw_preview()

    def zoom_view(self, event: tk.Event) -> None:
        self.zoom_view_delta(1.1 if event.delta > 0 else 1 / 1.1)

    def zoom_view_delta(self, factor: float) -> None:
        if self.preview_mode.get() != "3d":
            return
        self.view_zoom = max(0.25, min(8.0, self.view_zoom * factor))
        self.draw_preview()

    def rotation_values(self) -> tuple[float, float, float]:
        values = []
        for key in ("rotation_x", "rotation_y", "rotation_z"):
            try:
                values.append(float(self.entries[key].get()))
            except (KeyError, ValueError):
                values.append(0.0)
        return values[0], values[1], values[2]

    def refresh_model_preview(self) -> None:
        if not self.mesh:
            messagebox.showinfo("还没有模型", "请先打开 STL 或 OBJ 文件。", parent=self.root)
            return
        self.set_preview_mode("3d")
        self.status_var.set("3D 预览已更新；当前旋转角度会在开始切片时应用")

    def preview_mesh(self) -> Mesh:
        assert self.mesh is not None
        rotation = self.rotation_values()
        key = (id(self.mesh), *rotation)
        if self._preview_mesh_cache is not None and self._preview_mesh_cache_key == key:
            return self._preview_mesh_cache
        transformed = rotate_mesh(self.mesh, *rotation)
        self._preview_bounds = transformed.bounds
        triangles = transformed.triangles
        if len(triangles) > self.PREVIEW_TRIANGLE_LIMIT:
            step = max(1, math.ceil(len(triangles) / self.PREVIEW_TRIANGLE_LIMIT))
            triangles = triangles[::step][: self.PREVIEW_TRIANGLE_LIMIT]
        self._preview_mesh_cache = Mesh(triangles)
        self._preview_mesh_cache_key = key
        return self._preview_mesh_cache

    def settings(self) -> Settings:
        def number(key: str, minimum: float, integer: bool = False) -> float:
            try:
                value = float(self.entries[key].get())
            except ValueError as exc:
                raise ValueError(f"{key} 不是有效数字") from exc
            if value < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}")
            return int(value) if integer else value
        return Settings(
            layer_height=number("layer_height", 0.01), first_layer_height=number("first_layer_height", 0.01),
            gap=number("gap", 0.0), fill_enabled=True, fill_spacing=number("fill_spacing", 0.0),
            feed_speed=number("feed_speed", 0.1), travel_speed=number("travel_speed", 0.1),
            xy_feed_speed=number("xy_feed_speed", 0.1), xy_travel_speed=number("xy_travel_speed", 0.1),
            support_layers=int(number("support_layers", 0, True)), support_dwell=number("support_dwell", 0), exposure=number("exposure", 0),
            bed_x=number("bed_x", 1), bed_y=number("bed_y", 1), margin=number("margin", 0),
            rotation_x=number("rotation_x", -360), rotation_y=number("rotation_y", -360), rotation_z=number("rotation_z", -360),
            slice_direction=self.slice_direction or (0.0, 0.0, 1.0),
            auto_scale=self.auto_scale.get(), center_model=self.center_model.get(),
        )

    def slice(self) -> None:
        if not self.mesh:
            messagebox.showinfo("还没有模型", "请先打开 STL 或 OBJ 文件。", parent=self.root); return
        try:
            settings = self.settings()
            self.layers = slice_mesh(self.mesh, settings)
            for layer in self.layers[:settings.support_layers]:
                layer.material = "A"
            for layer in self.layers[settings.support_layers:]:
                layer.material = "B"
            self._settings = settings
            self.layer_scale.configure(to=max(1, len(self.layers)))
            self.layer_var.set(1)
            self.output_var.set("已生成预览，点击导出打印作业")
            self.status_var.set(f"切片完成：{len(self.layers)} 层 · 有效路径 {sum(len(layer.paths) for layer in self.layers):,} 条")
            self.draw_preview()
        except ValueError as exc:
            messagebox.showerror("切片参数错误", str(exc), parent=self.root)

    def _sync_layers(self) -> bool:
        """预览/导出前，把切片结果同步到界面上的当前参数。

        勾选「回字形填充」后如果没重新点「切片」，self._settings 仍是旧的，
        预览会照旧参数重建刀路 —— 表现就是内部空一大片。
        """
        try:
            current = self.settings()
        except ValueError as exc:
            messagebox.showerror("切片参数错误", str(exc), parent=self.root)
            return False
        if not self.layers or current != self._settings:
            self.slice()
        return bool(self.layers)

    def preview_toolpath(self) -> None:
        if not self._sync_layers():
            return
        try:
            from gcode_viewer_3d import parse_gcode

            preview_name = f"{Path(self.path_var.get()).stem or 'hkuprint'}_preview.gcode"
            gcode = build_gcode(self.layers, self._settings, Path("preview"), self.path_var.get())
            model = parse_gcode(gcode, preview_name)
            if not model.moves:
                raise ValueError("当前切片没有可预览的运动")
            ToolpathPreview(self.root, model)
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("无法打开刀路预览", str(exc), parent=self.root)

    def export(self) -> None:
        if not self._sync_layers():
            return
        if not self.output_directory:
            self.choose_output_directory()
        if not self.output_directory:
            return
        job_directory = self._next_job_directory()
        try:
            output = write_print_job(self.layers, self._settings, job_directory, self.path_var.get())
        except OSError as exc:
            messagebox.showerror("无法导出打印作业", str(exc), parent=self.root)
            return
        self.output_var.set(str(output))
        self.output_directory_var.set(f"作业根目录：{self.output_directory}")
        self.status_var.set(f"已导出 {len(self.layers)} 层到 {job_directory.name}：run.gcode + {len(self.layers)} 张掩码 PNG")

    def _next_job_directory(self) -> Path:
        assert self.output_directory is not None
        source_name = Path(self.path_var.get()).stem
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", source_name).strip(" .") or "hkuprint_job"
        candidate = self.output_directory / safe_name
        suffix = 2
        while candidate.exists():
            candidate = self.output_directory / f"{safe_name}_{suffix}"
            suffix += 1
        return candidate

    def draw_preview(self) -> None:
        self.canvas.delete("all")
        if self.preview_mode.get() == "3d":
            self.draw_model_3d()
            return
        if not self.layers:
            self.canvas.create_text(360, 250, text="切片后显示当前层轮廓", fill=self.MUTED, font=("Segoe UI", 16)); return
        index = max(1, min(len(self.layers), self.layer_var.get())) - 1
        layer = self.layers[index]
        self.layer_label.configure(text=f"{layer.number} / {len(self.layers)} · {layer.material} · Z {layer.z:.2f} mm")
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        bed_x = self._settings.bed_x if self._settings is not None else MACHINE_X
        bed_y = self._settings.bed_y if self._settings is not None else MACHINE_Y
        scale = min((width - 80) / bed_x, (height - 80) / bed_y)
        ox, oy = (width - bed_x * scale) / 2, (height + bed_y * scale) / 2
        self.canvas.create_rectangle(ox, oy - bed_y * scale, ox + bed_x * scale, oy, outline="#304552", width=2)
        self.canvas.create_text(ox + 8, oy - bed_y * scale + 8, text="DLP 工作区", anchor="nw", fill="#637985", font=("Segoe UI", 10))
        color = self.AMBER if layer.material == "A" else self.CYAN
        for path in layer.paths:
            points = []
            for x, y in path:
                points.extend((ox + x * scale, oy - y * scale))
            if len(points) >= 4:
                self.canvas.create_line(*points, fill=color, width=2, joinstyle="round")
        self.canvas.create_text(width - 18, 18, text=f"{layer.material} 层", anchor="ne", fill=color, font=("Segoe UI Semibold", 15))

    def draw_model_3d(self) -> None:
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        if not self.mesh:
            self.canvas.create_text(width / 2, height / 2, text="打开 STL / OBJ 后显示 3D 模型", fill=self.MUTED, font=("Segoe UI", 16))
            return
        rotation = self.rotation_values()
        mesh = self.preview_mesh()
        min_x, max_x, min_y, max_y, min_z, max_z = self._preview_bounds or mesh.bounds
        center = ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
        span = max(max_x - min_x, max_y - min_y, max_z - min_z, 1e-6)
        scale = min(width - 120, height - 120) / span * self.view_zoom
        yaw, pitch = math.radians(self.view_yaw), math.radians(self.view_pitch)
        sin_yaw, cos_yaw = math.sin(yaw), math.cos(yaw)
        sin_pitch, cos_pitch = math.sin(pitch), math.cos(pitch)

        def project(vertex: Vertex) -> tuple[float, float]:
            x, y, z = vertex.x - center[0], vertex.y - center[1], vertex.z - center[2]
            x, y = cos_yaw * x - sin_yaw * y, sin_yaw * x + cos_yaw * y
            y, z = cos_pitch * y - sin_pitch * z, sin_pitch * y + cos_pitch * z
            return width / 2 + x * scale, height / 2 - y * scale

        self._face_pick_candidates = []
        edges: set[tuple[tuple[float, float, float], tuple[float, float, float]]] = set()
        for triangle in mesh.triangles:
            vertices = (triangle.a, triangle.b, triangle.c)
            screen_triangle = tuple(project(vertex) for vertex in vertices)
            ab = (triangle.b.x - triangle.a.x, triangle.b.y - triangle.a.y, triangle.b.z - triangle.a.z)
            ac = (triangle.c.x - triangle.a.x, triangle.c.y - triangle.a.y, triangle.c.z - triangle.a.z)
            normal = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0])
            length = math.sqrt(sum(value * value for value in normal))
            if length > EPS:
                normal = tuple(value / length for value in normal)
                depth = sum(vertex.z for vertex in vertices) / 3
                self._face_pick_candidates.append((screen_triangle, normal, depth))
                self.canvas.create_polygon(*screen_triangle, fill="#10232c", outline="#1e4d5c", width=1)
            for left, right in ((vertices[0], vertices[1]), (vertices[1], vertices[2]), (vertices[2], vertices[0])):
                first = (round(left.x, 6), round(left.y, 6), round(left.z, 6))
                second = (round(right.x, 6), round(right.y, 6), round(right.z, 6))
                edges.add(tuple(sorted((first, second))))
        for first, second in edges:
            self.canvas.create_line(*project(Vertex(*first)), *project(Vertex(*second)), fill="#63b7d5", width=2)

        axis_length = span * 0.35
        origin = project(Vertex(center[0], center[1], center[2]))
        axes = (("X", Vertex(center[0] + axis_length, center[1], center[2]), "#ef6b73"),
                ("Y", Vertex(center[0], center[1] + axis_length, center[2]), "#64d391"),
                ("Z", Vertex(center[0], center[1], center[2] + axis_length), "#6fa8ff"))
        for label, endpoint, color in axes:
            point = project(endpoint)
            self.canvas.create_line(*origin, *point, fill=color, width=3, arrow=tk.LAST)
            self.canvas.create_text(point[0] + 8, point[1], text=label, fill=color, anchor="w", font=("Segoe UI Semibold", 13))
        self.canvas.create_text(16, 16, text=f"3D 模型 · 旋转 X {rotation[0]:.1f}°  Y {rotation[1]:.1f}°  Z {rotation[2]:.1f}°", anchor="nw", fill=self.TEXT, font=("Segoe UI Semibold", 13))
        slice_text = " · Z 切片方向已锁定" if self.slice_direction else ""
        preview_info = f"预览 {len(mesh.triangles):,}/{len(self.mesh.triangles):,} 面"
        self.canvas.create_text(width - 16, height - 16, text=f"{preview_info} · 视角 {self.view_yaw:.0f}° / {self.view_pitch:.0f}° · {self.view_zoom:.2f}×{slice_text}", anchor="se", fill=self.MUTED, font=("Segoe UI", 11))


def self_test() -> int:
    # Counter-clockwise corners on each Z plane make a closed, valid cube.
    a, b, c, d = (Vertex(0.0, 0.0, 0.0), Vertex(10.0, 0.0, 0.0),
                   Vertex(10.0, 10.0, 0.0), Vertex(0.0, 10.0, 0.0))
    e, f, g, h = (Vertex(0.0, 0.0, 2.0), Vertex(10.0, 0.0, 2.0),
                   Vertex(10.0, 10.0, 2.0), Vertex(0.0, 10.0, 2.0))
    vertices = [a, b, c, d, e, f, g, h]
    mesh = Mesh([Triangle(a, b, c), Triangle(a, c, d), Triangle(e, g, f), Triangle(e, h, g), Triangle(a, e, f), Triangle(a, f, b), Triangle(b, f, g), Triangle(b, g, c), Triangle(c, g, h), Triangle(c, h, d), Triangle(d, h, e), Triangle(d, e, a)])
    settings = Settings(layer_height=1.0, first_layer_height=1.0, auto_scale=False, center_model=False, bed_x=20, bed_y=20, margin=1)
    layers = slice_mesh(mesh, settings)
    assert len(layers) == 2 and all(layer.paths for layer in layers)
    fill = Settings(layer_height=1.0, first_layer_height=1.0, auto_scale=False, center_model=False, bed_x=20, bed_y=20, margin=1, fill_enabled=True, fill_spacing=1.0)
    fill_layers = slice_mesh(mesh, fill)
    assert len(_concentric_rings(fill_layers[0].paths[0], fill)) >= 3
    # 回字形环间距必须处处等于 fill_spacing，且不能出现塌缩后的退化环。
    rect = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0), (0.0, 0.0)]
    ring_settings = Settings(fill_enabled=True, fill_spacing=2.0)
    rect_rings = _concentric_rings(rect, ring_settings)
    assert len(rect_rings) == 5 and all(abs(_path_area(ring)) > 0.01 for ring in rect_rings)
    for outer, inner in zip(rect_rings, rect_rings[1:]):
        for axis in (0, 1):
            gap = min(p[axis] for p in inner) - min(p[axis] for p in outer)
            assert abs(gap - 2.0) < 1e-9, gap
    assert _offset_path(rect, 10.0) is None and _offset_path(rect, 1.0) is not None
    # 嵌套轮廓（外轮廓 + 孔 + 被孔包住的独立内岛）：内岛必须同样被回字形填充，
    # 而不是因为"整条轮廓都落在别的轮廓内部"被跳过、只剩一条描边。
    island_outer = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]
    island_hole = [(5.0, 5.0), (35.0, 5.0), (35.0, 35.0), (5.0, 35.0), (5.0, 5.0)][::-1]
    island_isle = [(12.0, 12.0), (28.0, 12.0), (28.0, 28.0), (12.0, 28.0), (12.0, 12.0)]
    island_layer = SliceLayer(1, 0.05, "A", [island_outer, island_hole, island_isle])
    island_gcode = build_gcode([island_layer], Settings(fill_enabled=True, fill_spacing=1.0), Path("."), "island")
    island_rings = island_gcode.split(";LAYER_START:0")[1].split(";LAYER_END")[0].count("M101")
    assert island_rings >= 12, island_rings  # 外 5 + 孔 1 + 岛 >= 6（修复前岛仅 1）
    # 长边上夹着冗余共线点（切片常见）时，等距偏移必须越过共线短边继续填充，
    # 而不是在短边反向时塌缩、留下离孔一大截的空档。
    notch_outer = [(0.0, 0.0), (23.0, 0.0), (23.0, 1.5), (23.0, 23.0), (1.5, 23.0), (0.0, 23.0), (0.0, 1.5), (0.0, 0.0)]
    notch_hole = [(4.0, 4.0), (19.0, 4.0), (19.0, 5.0), (19.0, 19.0), (5.0, 19.0), (4.0, 19.0), (4.0, 5.0), (4.0, 4.0)][::-1]
    notch_layer = SliceLayer(1, 0.05, "A", [notch_outer, notch_hole])
    notch_gcode = build_gcode([notch_layer], Settings(fill_enabled=True, fill_spacing=0.5), Path("."), "notch")
    notch_rings = notch_gcode.split(";LAYER_START:0")[1].split(";LAYER_END")[0].count("M101")
    assert notch_rings >= 9, notch_rings  # 孔 1 + 外 1 + 填充 7（修复前外仅 1 环就塌缩）
    with tempfile.TemporaryDirectory() as directory:
        output = write_print_job(layers, settings, Path(directory), "self-test.stl")
        mask = output.parent / "1.png"
        data = mask.read_bytes()
        assert output.name == "run.gcode" and b'M6054 "1.png"' in output.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", data[16:24]) == (MASK_WIDTH, MASK_HEIGHT)
    print(f"OK slicer: triangles={len(mesh.triangles)} layers={len(layers)} paths={sum(len(layer.paths) for layer in layers)} masks={len(layers)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HKUprint contour G-code slicer")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    root = tk.Tk()
    SlicerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
