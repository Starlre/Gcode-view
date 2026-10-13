"""JSON-lines bridge to the existing offline slicer. Never opens a serial port."""
from __future__ import annotations

import dataclasses
import json
import math
import re
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import gcode_slicer as core

PREVIEW_TRIANGLES = 20000
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)


def settings_from_json(values: dict) -> core.Settings:
    if not isinstance(values, dict):
        raise ValueError("切片参数必须是对象")
    names = {f.name for f in dataclasses.fields(core.Settings)}
    if set(values) - names:
        raise ValueError("存在未知切片参数")
    settings = core.Settings(**values)
    numeric_fields = {
        "layer_height": (0.01, 20), "first_layer_height": (0.01, 20),
        "fill_spacing": (0.01, 20), "gap": (0, 20),
        "feed_speed": (0.1, 3000), "travel_speed": (0.1, 3000),
        "xy_feed_speed": (0.1, 3000), "xy_travel_speed": (0.1, 3000),
        "bed_x": (1, 1000), "bed_y": (1, 1000), "margin": (0, 100),
        "support_layers": (0, 10000), "support_dwell": (0, 86400),
        "exposure": (0, 86400), "rotation_x": (-360, 360),
        "rotation_y": (-360, 360), "rotation_z": (-360, 360),
    }
    for name, (minimum, maximum) in numeric_fields.items():
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"{name} 必须为有效数字")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    if int(settings.support_layers) != settings.support_layers:
        raise ValueError("底层数量必须为整数")
    if settings.margin * 2 >= min(settings.bed_x, settings.bed_y):
        raise ValueError("边距必须小于工作区最短边的一半")
    if not isinstance(settings.slice_direction, (list, tuple)) or len(settings.slice_direction) != 3:
        raise ValueError("切片方向需要三个分量")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in settings.slice_direction):
        raise ValueError("切片方向无效")
    if math.sqrt(sum(v * v for v in settings.slice_direction)) < 1e-7:
        raise ValueError("切片方向不能为零向量")
    settings.support_layers = int(settings.support_layers)
    settings.slice_direction = tuple(settings.slice_direction)
    settings.fill_enabled = True
    return settings


def cube_mesh(size: float = 15.0) -> core.Mesh:
    vertices = [core.Vertex(x, y, z) for z in (0, size) for y in (0, size) for x in (0, size)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6),
             (0, 1, 5), (0, 5, 4), (2, 6, 7), (2, 7, 3),
             (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return core.Mesh([core.Triangle(*(vertices[i] for i in face)) for face in faces])


class Backend:
    def __init__(self):
        self.mesh = None
        self.model_name = ""
        self.layers = None
        self.settings = None
        self.gcode = None

    def mesh_result(self):
        mesh = self.mesh
        step = max(1, math.ceil(len(mesh.triangles) / PREVIEW_TRIANGLES))
        triangles = mesh.triangles[::step][:PREVIEW_TRIANGLES]
        return {
            "kind": "model", "name": self.model_name,
            "triangles": len(mesh.triangles), "previewTriangles": len(triangles),
            "bounds": mesh.bounds,
            "positions": [v for t in triangles for p in (t.a, t.b, t.c) for v in (p.x, p.y, p.z)],
        }

    def handle(self, method, args):
        if method == "info":
            return {"engine": "Python · HKUprint", "mask": [core.MASK_WIDTH, core.MASK_HEIGHT]}
        if method in ("load", "demo"):
            if method == "demo":
                mesh, name = cube_mesh(), "示例方块 · 15 mm"
            else:
                path = Path(args["path"])
                mesh, name = core.load_mesh(path), path.name
            # Keep the previous model/job if importing a file fails.
            self.mesh, self.model_name = mesh, name
            self.layers = self.settings = self.gcode = None
            return self.mesh_result()
        if method == "slice":
            if self.mesh is None:
                raise ValueError("请先导入 STL 或 OBJ 模型")
            settings = settings_from_json(args["settings"])
            emit({"event": "progress", "message": "正在计算模型截面…"})
            layers = core.slice_mesh(self.mesh, settings)
            if not layers or not any(layer.paths for layer in layers):
                raise ValueError("没有得到有效截面，请检查模型是否闭合或调整切片方向")
            for layer in layers:
                layer.material = "A" if layer.number <= settings.support_layers else "B"
            emit({"event": "progress", "message": f"正在生成 {len(layers)} 层回字形走刀…"})
            gcode = core.build_gcode(layers, settings, Path("preview"), self.model_name)
            self.layers, self.settings, self.gcode = layers, settings, gcode
            return {"kind": "toolpath", "name": self.model_name, "gcode": gcode,
                    "layerCount": len(layers), "contours": sum(len(layer.paths) for layer in layers)}
        if method == "export":
            if not self.layers or self.settings is None:
                raise ValueError("请先完成切片，再导出打印作业")
            root = Path(args["directory"])
            if not root.is_dir():
                raise ValueError("作业根目录不存在")
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(self.model_name).stem).strip(" .") or "hkuprint_job"
            target = root / name
            suffix = 2
            while target.exists():
                target = root / f"{name}_{suffix}"
                suffix += 1
            emit({"event": "progress", "message": f"正在导出 {len(self.layers)} 张曝光掩码…"})
            output = core.write_print_job(self.layers, self.settings, target, self.model_name)
            return {"directory": str(target), "gcode": str(output), "layers": len(self.layers)}
        raise ValueError("未知后端操作")


def main():
    backend = Backend()
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            result = backend.handle(request["method"], request.get("args", {}))
            emit({"id": request["id"], "ok": True, "data": result})
        except Exception as exc:
            emit({"id": request.get("id"), "ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()
