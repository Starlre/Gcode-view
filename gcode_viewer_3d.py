"""Native Windows OpenGL G-code viewer with a Tkinter desktop GUI.

No third-party runtime packages are required. Tkinter owns the controls and a
Win32 WGL context renders directly into the center viewport.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import gc
import math
import re
import statistics
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


if sys.platform != "win32":
    raise RuntimeError("The native OpenGL viewer currently supports Windows only.")


@dataclass(slots=True)
class Point3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0


@dataclass(slots=True)
class Move:
    start: Point3
    end: Point3
    command: str
    source: str
    line_number: int
    feed: float
    duration: float
    distance: float
    layer: str
    travel: bool = True
    dwell: bool = False
    light: float = 0.0
    time_start: float = 0.0
    time_end: float = 0.0


@dataclass(slots=True)
class Layer:
    name: str
    first_move: int
    last_move: int


@dataclass(slots=True)
class GCodeModel:
    name: str
    moves: list[Move] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    line_count: int = 0
    total_distance: float = 0.0
    total_duration: float = 0.0
    time_ends: list[float] = field(default_factory=list)
    min_point: Point3 = field(default_factory=Point3)
    max_point: Point3 = field(default_factory=Point3)


NUMBER_RE = re.compile(r"([XYZEFIJKRPS])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)
COMMAND_RE = re.compile(r"^\s*([GMT])\s*(-?\d+(?:\.\d+)?)", re.I)
LAYER_RE = re.compile(r";\s*(?:LAYER_START|LAYER)\s*:?[\s]*(-?\d+)", re.I)
META_RE = re.compile(r"^;\s*([^:]+):\s*(.*)$")


def _strip_comments(line: str) -> str:
    return re.sub(r"\([^)]*\)", "", line.split(";", 1)[0]).strip()


def _arc_points(start: Point3, end: Point3, args: dict[str, float], clockwise: bool) -> list[Point3]:
    """Approximate common XY I/J or R arcs with short line segments."""
    if "I" in args or "J" in args:
        cx, cy = start.x + args.get("I", 0.0), start.y + args.get("J", 0.0)
    elif "R" in args:
        radius = abs(args["R"])
        dx, dy = end.x - start.x, end.y - start.y
        chord = math.hypot(dx, dy)
        if chord <= 1e-9 or chord > 2 * radius:
            return [end]
        mx, my = (start.x + end.x) / 2, (start.y + end.y) / 2
        height = math.sqrt(max(0.0, radius * radius - (chord / 2) ** 2))
        nx, ny = -dy / chord, dx / chord
        sign = -1.0 if clockwise else 1.0
        if args["R"] < 0:
            sign *= -1.0
        cx, cy = mx + nx * height * sign, my + ny * height * sign
    else:
        return [end]

    start_angle = math.atan2(start.y - cy, start.x - cx)
    end_angle = math.atan2(end.y - cy, end.x - cx)
    sweep = end_angle - start_angle
    if clockwise and sweep >= 0:
        sweep -= math.tau
    if not clockwise and sweep <= 0:
        sweep += math.tau
    radius = math.hypot(start.x - cx, start.y - cy)
    steps = max(8, min(256, int(abs(sweep) * max(radius, 1.0) / 0.7)))
    points = []
    for step in range(1, steps + 1):
        ratio = step / steps
        angle = start_angle + sweep * ratio
        points.append(
            Point3(
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
                start.z + (end.z - start.z) * ratio,
                start.e + (end.e - start.e) * ratio,
            )
        )
    points[-1] = end
    return points


def parse_gcode(text: str, name: str = "untitled.gcode") -> GCodeModel:
    lines = text.lstrip("\ufeff").splitlines()
    model = GCodeModel(name=name, line_count=len(lines))
    position = Point3()
    feed = 1200.0
    absolute_xyz = True
    absolute_e = True
    units = 1.0
    light = 0.0
    tool_active = False
    active_layer: str | None = None
    layer_ranges: dict[str, list[int]] = {}

    def register(move: Move) -> None:
        move.time_start = model.total_duration
        model.total_duration += move.duration
        move.time_end = model.total_duration
        model.total_distance += move.distance
        model.moves.append(move)
        model.time_ends.append(move.time_end)
        if move.layer:
            span = layer_ranges.setdefault(move.layer, [len(model.moves) - 1, len(model.moves) - 1])
            span[1] = len(model.moves) - 1

    for line_number, raw in enumerate(lines, 1):
        metadata = META_RE.match(raw.strip())
        if metadata:
            model.metadata[metadata.group(1).strip()] = metadata.group(2).strip()
        layer_match = LAYER_RE.search(raw)
        if layer_match:
            active_layer = layer_match.group(1)

        code = _strip_comments(raw)
        match = COMMAND_RE.match(code)
        if not match:
            continue
        command = f"{match.group(1).upper()}{match.group(2)}"
        args = {key.upper(): float(value) for key, value in NUMBER_RE.findall(code[match.end() :])}

        if command == "G20":
            units = 25.4
            continue
        if command == "G21":
            units = 1.0
            continue
        if command == "G90":
            absolute_xyz = True
            continue
        if command == "G91":
            absolute_xyz = False
            continue
        if command == "M82":
            absolute_e = True
            continue
        if command == "M83":
            absolute_e = False
            continue
        if command in {"M3", "M106"}:
            light = args.get("S", 255.0)
            continue
        if command in {"M5", "M107"}:
            light = 0.0
            continue
        if command == "M101":
            tool_active = True
            continue
        if command == "M102":
            tool_active = False
            continue
        if command == "G92":
            position = Point3(
                args.get("X", position.x / units) * units,
                args.get("Y", position.y / units) * units,
                args.get("Z", position.z / units) * units,
                args.get("E", position.e / units) * units,
            )
            continue
        if command == "G28":
            axes = {axis for axis in "XYZ" if axis in args or re.search(rf"\b{axis}\b", code, re.I)}
            if not axes:
                axes = set("XYZ")
            position = Point3(
                0.0 if "X" in axes else position.x,
                0.0 if "Y" in axes else position.y,
                0.0 if "Z" in axes else position.z,
                position.e,
            )
            continue
        if command == "G4":
            duration = args.get("P", args.get("S", 0.0) * 1000.0) / 1000.0
            if duration > 0:
                register(
                    Move(position, position, command, raw.strip(), line_number, feed, duration, 0.0,
                         active_layer or "", travel=False, dwell=True, light=light)
                )
            continue
        if command not in {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"}:
            continue

        if "F" in args:
            feed = max(args["F"] * units, 0.001)

        def axis_value(axis: str, current: float, absolute: bool) -> float:
            if axis not in args:
                return current
            value = args[axis] * units
            return value if absolute else current + value

        target = Point3(
            axis_value("X", position.x, absolute_xyz),
            axis_value("Y", position.y, absolute_xyz),
            axis_value("Z", position.z, absolute_xyz),
            axis_value("E", position.e, absolute_e),
        )
        is_arc = command in {"G2", "G02", "G3", "G03"}
        points = _arc_points(position, target, {k: v * units for k, v in args.items()}, command in {"G2", "G02"}) if is_arc else [target]
        previous = position
        source_e_delta = target.e - position.e
        for point in points:
            distance = math.sqrt(
                (point.x - previous.x) ** 2 + (point.y - previous.y) ** 2 + (point.z - previous.z) ** 2
            )
            if distance <= 1e-9:
                previous = point
                continue
            travel = command in {"G0", "G00"} or (source_e_delta <= 1e-9 and not tool_active)
            register(
                Move(previous, point, command, raw.strip(), line_number, feed,
                     distance / (feed / 60.0), distance, active_layer or "", travel=travel, light=light)
            )
            previous = point
        position = target

    if not layer_ranges and model.moves:
        for index, move in enumerate(model.moves):
            layer_name = f"Z {move.end.z:.3f}"
            move.layer = layer_name
            span = layer_ranges.setdefault(layer_name, [index, index])
            span[1] = index
    model.layers = [Layer(name, first, last) for name, (first, last) in layer_ranges.items()]

    points = [point for move in model.moves for point in (move.start, move.end)]
    if points:
        model.min_point = Point3(
            min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)
        )
        model.max_point = Point3(
            max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)
        )
    return model


# OpenGL 1.1 is part of Windows. Fixed-function rendering gives this utility a
# zero-install runtime and is sufficient for line-based toolpaths.
GL = ctypes.windll.opengl32
GLU = ctypes.windll.glu32
GDI = ctypes.windll.gdi32
USER = ctypes.windll.user32
WINMM = ctypes.windll.winmm

GL_COLOR_BUFFER_BIT = 0x00004000
GL_DEPTH_BUFFER_BIT = 0x00000100
GL_LINES = 0x0001
GL_LINE_STRIP = 0x0003
GL_TRIANGLES = 0x0004
GL_TRIANGLE_FAN = 0x0006
GL_POINTS = 0x0000
GL_QUADS = 0x0007
GL_MODELVIEW = 0x1700
GL_PROJECTION = 0x1701
GL_DEPTH_TEST = 0x0B71
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_FRONT = 0x0404
GL_RGB = 0x1907
GL_UNSIGNED_BYTE = 0x1401
GL_PACK_ALIGNMENT = 0x0D05
GL_SMOOTH = 0x1D01
GL_LINE_SMOOTH = 0x0B20
GL_POINT_SMOOTH = 0x0B10
GL_LEQUAL = 0x0203
GL_COMPILE = 0x1300
PFD_DRAW_TO_WINDOW = 0x00000004
PFD_SUPPORT_OPENGL = 0x00000020
PFD_DOUBLEBUFFER = 0x00000001
PFD_TYPE_RGBA = 0
PFD_MAIN_PLANE = 0


class PIXELFORMATDESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("nSize", wintypes.WORD), ("nVersion", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("iPixelType", ctypes.c_ubyte), ("cColorBits", ctypes.c_ubyte), ("cRedBits", ctypes.c_ubyte),
        ("cRedShift", ctypes.c_ubyte), ("cGreenBits", ctypes.c_ubyte), ("cGreenShift", ctypes.c_ubyte),
        ("cBlueBits", ctypes.c_ubyte), ("cBlueShift", ctypes.c_ubyte), ("cAlphaBits", ctypes.c_ubyte),
        ("cAlphaShift", ctypes.c_ubyte), ("cAccumBits", ctypes.c_ubyte), ("cAccumRedBits", ctypes.c_ubyte),
        ("cAccumGreenBits", ctypes.c_ubyte), ("cAccumBlueBits", ctypes.c_ubyte), ("cAccumAlphaBits", ctypes.c_ubyte),
        ("cDepthBits", ctypes.c_ubyte), ("cStencilBits", ctypes.c_ubyte), ("cAuxBuffers", ctypes.c_ubyte),
        ("iLayerType", ctypes.c_ubyte), ("bReserved", ctypes.c_ubyte), ("dwLayerMask", wintypes.DWORD),
        ("dwVisibleMask", wintypes.DWORD), ("dwDamageMask", wintypes.DWORD),
    ]


def _configure_gl_signatures() -> None:
    for name, args in {
        "glClearColor": [ctypes.c_float] * 4,
        "glClear": [ctypes.c_uint],
        "glEnable": [ctypes.c_uint],
        "glDisable": [ctypes.c_uint],
        "glBlendFunc": [ctypes.c_uint, ctypes.c_uint],
        "glDepthFunc": [ctypes.c_uint],
        "glShadeModel": [ctypes.c_uint],
        "glViewport": [ctypes.c_int] * 4,
        "glMatrixMode": [ctypes.c_uint],
        "glLoadIdentity": [],
        "glTranslatef": [ctypes.c_float] * 3,
        "glRotatef": [ctypes.c_float] * 4,
        "glBegin": [ctypes.c_uint],
        "glEnd": [],
        "glVertex3f": [ctypes.c_float] * 3,
        "glColor3f": [ctypes.c_float] * 3,
        "glColor4f": [ctypes.c_float] * 4,
        "glLineWidth": [ctypes.c_float],
        "glPointSize": [ctypes.c_float],
        "glReadBuffer": [ctypes.c_uint],
        "glReadPixels": [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p],
        "glPixelStorei": [ctypes.c_uint, ctypes.c_int],
        "glFinish": [],
        "glNewList": [ctypes.c_uint, ctypes.c_uint],
        "glEndList": [],
        "glCallList": [ctypes.c_uint],
        "glDeleteLists": [ctypes.c_uint, ctypes.c_int],
    }.items():
        getattr(GL, name).argtypes = args
    GLU.gluPerspective.argtypes = [ctypes.c_double] * 4
    GL.glGenLists.argtypes = [ctypes.c_int]
    GL.glGenLists.restype = ctypes.c_uint
    USER.GetDC.argtypes = [wintypes.HWND]
    USER.GetDC.restype = wintypes.HDC
    USER.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    GDI.ChoosePixelFormat.argtypes = [wintypes.HDC, ctypes.POINTER(PIXELFORMATDESCRIPTOR)]
    GDI.SetPixelFormat.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.POINTER(PIXELFORMATDESCRIPTOR)]
    GDI.SwapBuffers.argtypes = [wintypes.HDC]
    GL.wglCreateContext.argtypes = [wintypes.HDC]
    GL.wglCreateContext.restype = wintypes.HANDLE
    GL.wglMakeCurrent.argtypes = [wintypes.HDC, wintypes.HANDLE]
    GL.wglDeleteContext.argtypes = [wintypes.HANDLE]
    WINMM.timeBeginPeriod.argtypes = [wintypes.UINT]
    WINMM.timeBeginPeriod.restype = wintypes.UINT
    WINMM.timeEndPeriod.argtypes = [wintypes.UINT]
    WINMM.timeEndPeriod.restype = wintypes.UINT


_configure_gl_signatures()


class OpenGLViewport(tk.Frame):
    PROGRESS_CHUNK_SIZE = 512

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, bg="#090e13", highlightthickness=0, **kwargs)
        self.model: GCodeModel | None = None
        self.progress_time = 0.0
        self.selected_layer = 0
        self.show_travel = True
        self.show_grid = True
        self.show_machine = True
        self.pitch = 55.0
        self.yaw = -35.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.camera_distance = 180.0
        self.center = Point3()
        self.scene_size = 100.0
        self._dc: int | None = None
        self._rc: int | None = None
        self._mouse = (0, 0)
        self._mouse_button = 0
        self._initialized = False
        self._rendering = False
        self.last_frame_ms = 0.0
        self.last_swap_ms = 0.0
        self._path_list = 0
        self._path_cache_key: tuple[int, bool, int] | None = None
        self._history_list = 0
        self._history_cache_key: tuple[int, int] | None = None
        self._progress_list_base = 0
        self._progress_list_count = 0
        self._progress_cache_key: tuple[int, int, int, int] | None = None
        self.bind("<Map>", self._ensure_context)
        self.bind("<Configure>", lambda _event: self.render())
        self.bind("<ButtonPress-1>", lambda event: self._press(event, 1))
        self.bind("<ButtonPress-2>", lambda event: self._press(event, 2))
        self.bind("<ButtonPress-3>", lambda event: self._press(event, 3))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<ButtonRelease-2>", self._release)
        self.bind("<ButtonRelease-3>", self._release)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<B2-Motion>", self._drag)
        self.bind("<B3-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Double-Button-1>", lambda _event: self.fit_view())

    def _ensure_context(self, _event: tk.Event | None = None) -> None:
        if self._initialized or not self.winfo_ismapped():
            return
        self.update_idletasks()
        hwnd = self.winfo_id()
        self._dc = USER.GetDC(hwnd)
        descriptor = PIXELFORMATDESCRIPTOR()
        descriptor.nSize = ctypes.sizeof(PIXELFORMATDESCRIPTOR)
        descriptor.nVersion = 1
        descriptor.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER
        descriptor.iPixelType = PFD_TYPE_RGBA
        descriptor.cColorBits = 32
        descriptor.cDepthBits = 24
        descriptor.iLayerType = PFD_MAIN_PLANE
        pixel_format = GDI.ChoosePixelFormat(self._dc, ctypes.byref(descriptor))
        if not pixel_format or not GDI.SetPixelFormat(self._dc, pixel_format, ctypes.byref(descriptor)):
            raise OSError("Unable to set an OpenGL pixel format for the viewport.")
        self._rc = GL.wglCreateContext(self._dc)
        if not self._rc or not GL.wglMakeCurrent(self._dc, self._rc):
            raise OSError("Unable to create the OpenGL rendering context.")
        GL.glClearColor(0.035, 0.055, 0.074, 1.0)
        GL.glEnable(GL_DEPTH_TEST)
        GL.glDepthFunc(GL_LEQUAL)
        GL.glEnable(GL_BLEND)
        GL.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        # Legacy GL_LINE_SMOOTH is unusually expensive on Windows OpenGL 1.1
        # drivers for dense toolpaths. Wide aliased lines remain clear and stable.
        GL.glDisable(GL_LINE_SMOOTH)
        GL.glEnable(GL_POINT_SMOOTH)
        GL.glShadeModel(GL_SMOOTH)
        self._initialized = True
        self.render()

    def destroy(self) -> None:
        if self._path_list and self._rc:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._path_list, 1)
            self._path_list = 0
        if self._history_list and self._rc:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._history_list, 1)
            self._history_list = 0
        if self._progress_list_base and self._rc:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._progress_list_base, self._progress_list_count)
            self._progress_list_base = 0
            self._progress_list_count = 0
        if self._rc:
            GL.wglMakeCurrent(0, 0)
            GL.wglDeleteContext(self._rc)
            self._rc = None
        if self._dc:
            USER.ReleaseDC(self.winfo_id(), self._dc)
            self._dc = None
        super().destroy()

    def set_model(self, model: GCodeModel) -> None:
        self.invalidate_path_cache()
        self.model = model
        self.progress_time = 0.0
        self.selected_layer = 0
        self.fit_view()

    def fit_view(self) -> None:
        if self.model:
            low, high = self.model.min_point, self.model.max_point
            self.center = Point3((low.x + high.x) / 2, (low.y + high.y) / 2, (low.z + high.z) / 2)
            self.scene_size = max(high.x - low.x, high.y - low.y, high.z - low.z, 10.0)
            self.camera_distance = self.scene_size * 2.25
        self.pan_x = self.pan_y = 0.0
        self.pitch, self.yaw = 55.0, -35.0
        self.render()

    def set_preset(self, preset: str) -> None:
        settings = {"iso": (55.0, -35.0), "top": (0.0, 0.0), "front": (90.0, 0.0), "side": (90.0, 90.0)}
        self.pitch, self.yaw = settings[preset]
        self.pan_x = self.pan_y = 0.0
        self.render()

    def _press(self, event: tk.Event, button: int) -> None:
        self.focus_set()
        self._mouse = (event.x, event.y)
        self._mouse_button = button

    def _release(self, _event: tk.Event) -> None:
        self._mouse_button = 0

    def _drag(self, event: tk.Event) -> None:
        dx, dy = event.x - self._mouse[0], event.y - self._mouse[1]
        self._mouse = (event.x, event.y)
        if self._mouse_button == 1:
            self.yaw += dx * 0.45
            self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.45))
        else:
            scale = self.camera_distance / max(200.0, self.winfo_height())
            self.pan_x += dx * scale
            self.pan_y -= dy * scale
        self.render()

    def _wheel(self, event: tk.Event) -> None:
        self.camera_distance *= 0.88 if event.delta > 0 else 1.14
        self.camera_distance = max(self.scene_size * 0.08, min(self.scene_size * 60.0, self.camera_distance))
        self.render()

    @staticmethod
    def _vertex(point: Point3) -> None:
        GL.glVertex3f(point.x, point.y, point.z)

    @staticmethod
    def _cylinder_z(
        x: float,
        y: float,
        bottom: float,
        top: float,
        radius: float,
        color: tuple[float, float, float, float],
        sides: int = 18,
    ) -> None:
        GL.glBegin(GL_QUADS)
        for index in range(sides):
            angle_a = math.tau * index / sides
            angle_b = math.tau * (index + 1) / sides
            shade_a = 0.68 + 0.32 * (math.sin(angle_a) + 1.0) / 2.0
            shade_b = 0.68 + 0.32 * (math.sin(angle_b) + 1.0) / 2.0
            ax, ay = x + radius * math.cos(angle_a), y + radius * math.sin(angle_a)
            bx, by = x + radius * math.cos(angle_b), y + radius * math.sin(angle_b)
            GL.glColor4f(color[0] * shade_a, color[1] * shade_a, color[2] * shade_a, color[3])
            GL.glVertex3f(ax, ay, bottom); GL.glVertex3f(ax, ay, top)
            GL.glColor4f(color[0] * shade_b, color[1] * shade_b, color[2] * shade_b, color[3])
            GL.glVertex3f(bx, by, top); GL.glVertex3f(bx, by, bottom)
        GL.glEnd()
        for height, reverse in ((bottom, True), (top, False)):
            GL.glColor4f(*color)
            GL.glBegin(GL_TRIANGLE_FAN)
            GL.glVertex3f(x, y, height)
            indices = range(sides, -1, -1) if reverse else range(sides + 1)
            for index in indices:
                angle = math.tau * index / sides
                GL.glVertex3f(x + radius * math.cos(angle), y + radius * math.sin(angle), height)
            GL.glEnd()

    @staticmethod
    def _cone_tip(
        x: float,
        y: float,
        base_z: float,
        tip_z: float,
        radius: float,
        color: tuple[float, float, float, float],
        sides: int = 18,
    ) -> None:
        GL.glBegin(GL_TRIANGLES)
        for index in range(sides):
            angle_a = math.tau * index / sides
            angle_b = math.tau * (index + 1) / sides
            GL.glColor4f(*color)
            GL.glVertex3f(x, y, tip_z)
            GL.glVertex3f(x + radius * math.cos(angle_a), y + radius * math.sin(angle_a), base_z)
            GL.glVertex3f(x + radius * math.cos(angle_b), y + radius * math.sin(angle_b), base_z)
        GL.glEnd()

    def _current_move(self) -> tuple[int, Move | None, Point3]:
        if not self.model or not self.model.moves:
            return 0, None, Point3()
        index = min(len(self.model.moves) - 1, bisect.bisect_right(self.model.time_ends, self.progress_time))
        move = self.model.moves[index]
        ratio = 1.0 if move.duration <= 0 else max(0.0, min(1.0, (self.progress_time - move.time_start) / move.duration))
        point = Point3(
            move.start.x + (move.end.x - move.start.x) * ratio,
            move.start.y + (move.end.y - move.start.y) * ratio,
            move.start.z + (move.end.z - move.start.z) * ratio,
            move.start.e + (move.end.e - move.start.e) * ratio,
        )
        return index, move, point

    def _layer_visible(self, move: Move) -> bool:
        if self.selected_layer <= 0 or not self.model:
            return True
        return move.layer == self.model.layers[self.selected_layer - 1].name

    def invalidate_path_cache(self) -> None:
        self._path_cache_key = None
        self._history_cache_key = None
        self._progress_cache_key = None
        if self._path_list and self._initialized:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._path_list, 1)
            self._path_list = 0
        if self._history_list and self._initialized:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._history_list, 1)
            self._history_list = 0
        if self._progress_list_base and self._initialized:
            GL.wglMakeCurrent(self._dc, self._rc)
            GL.glDeleteLists(self._progress_list_base, self._progress_list_count)
            self._progress_list_base = 0
            self._progress_list_count = 0

    def _draw_static_path(self) -> None:
        if not self.model:
            return
        cache_key = (id(self.model), self.show_travel, self.selected_layer)
        if self._path_cache_key != cache_key or not self._path_list:
            self.invalidate_path_cache()
            self._path_list = GL.glGenLists(1)
            GL.glNewList(self._path_list, GL_COMPILE)
            GL.glLineWidth(1.25)
            GL.glBegin(GL_LINES)
            for move in self.model.moves:
                if move.dwell or not self._layer_visible(move) or (move.travel and not self.show_travel):
                    continue
                if move.travel:
                    GL.glColor4f(0.45, 0.55, 0.61, 0.34)
                else:
                    GL.glColor4f(0.28, 0.69, 0.98, 0.88)
                self._vertex(move.start); self._vertex(move.end)
            GL.glEnd()
            GL.glEndList()
            self._path_cache_key = cache_key
        GL.glCallList(self._path_list)

    def _draw_grid(self) -> None:
        if not self.model:
            return
        low, high = self.model.min_point, self.model.max_point
        span = max(high.x - low.x, high.y - low.y, 20.0)
        step = 1.0
        while span / step > 28:
            step *= 2.0 if int(math.log10(step)) % 2 else 2.5
        min_x, max_x = math.floor((low.x - span * .12) / step) * step, math.ceil((high.x + span * .12) / step) * step
        min_y, max_y = math.floor((low.y - span * .12) / step) * step, math.ceil((high.y + span * .12) / step) * step
        z = low.z
        GL.glColor4f(0.24, 0.34, 0.40, 0.42)
        GL.glLineWidth(1.0)
        GL.glBegin(GL_LINES)
        value = min_x
        while value <= max_x + step / 2:
            GL.glVertex3f(value, min_y, z)
            GL.glVertex3f(value, max_y, z)
            value += step
        value = min_y
        while value <= max_y + step / 2:
            GL.glVertex3f(min_x, value, z)
            GL.glVertex3f(max_x, value, z)
            value += step
        GL.glEnd()

    @staticmethod
    def _draw_axis_lines(origin: Point3, length: float, line_width: float) -> None:
        """Draw RGB XYZ axes with arrowheads."""
        head = length * 0.13
        wing = head * 0.42
        GL.glLineWidth(line_width)
        GL.glBegin(GL_LINES)
        GL.glColor3f(1.0, 0.30, 0.36)
        GL.glVertex3f(origin.x, origin.y, origin.z); GL.glVertex3f(origin.x + length, origin.y, origin.z)
        GL.glVertex3f(origin.x + length, origin.y, origin.z); GL.glVertex3f(origin.x + length - head, origin.y + wing, origin.z)
        GL.glVertex3f(origin.x + length, origin.y, origin.z); GL.glVertex3f(origin.x + length - head, origin.y - wing, origin.z)
        GL.glColor3f(0.28, 0.94, 0.55)
        GL.glVertex3f(origin.x, origin.y, origin.z); GL.glVertex3f(origin.x, origin.y + length, origin.z)
        GL.glVertex3f(origin.x, origin.y + length, origin.z); GL.glVertex3f(origin.x + wing, origin.y + length - head, origin.z)
        GL.glVertex3f(origin.x, origin.y + length, origin.z); GL.glVertex3f(origin.x - wing, origin.y + length - head, origin.z)
        GL.glColor3f(0.30, 0.68, 1.0)
        GL.glVertex3f(origin.x, origin.y, origin.z); GL.glVertex3f(origin.x, origin.y, origin.z + length)
        GL.glVertex3f(origin.x, origin.y, origin.z + length); GL.glVertex3f(origin.x + wing, origin.y, origin.z + length - head)
        GL.glVertex3f(origin.x, origin.y, origin.z + length); GL.glVertex3f(origin.x - wing, origin.y, origin.z + length - head)
        GL.glEnd()

    def _draw_axes(self) -> None:
        length = max(self.scene_size * 0.25, 10.0)
        self._draw_axis_lines(Point3(), length, 3.5)

    def _draw_orientation_gizmo(self, width: int, height: int) -> None:
        size = max(112, min(148, int(min(width, height) * 0.17)))
        GL.glViewport(14, 14, size, size)
        GL.glMatrixMode(GL_PROJECTION)
        GL.glLoadIdentity()
        GLU.gluPerspective(32.0, 1.0, 0.1, 20.0)
        GL.glMatrixMode(GL_MODELVIEW)
        GL.glLoadIdentity()
        GL.glTranslatef(0.0, 0.0, -5.0)
        GL.glRotatef(self.pitch, 1.0, 0.0, 0.0)
        GL.glRotatef(self.yaw, 0.0, 0.0, 1.0)
        GL.glDisable(GL_DEPTH_TEST)
        self._draw_axis_lines(Point3(), 1.65, 4.0)
        GL.glEnable(GL_DEPTH_TEST)
        GL.glViewport(0, 0, width, height)

    def _draw_machine(self, current: Point3, exposing: bool) -> None:
        if not self.model:
            return
        low, high = self.model.min_point, self.model.max_point
        pad = self.scene_size * 0.08
        z = low.z - self.scene_size * 0.008
        GL.glColor4f(0.10, 0.15, 0.19, 0.58)
        GL.glBegin(GL_QUADS)
        GL.glVertex3f(low.x-pad, low.y-pad, z); GL.glVertex3f(high.x+pad, low.y-pad, z)
        GL.glVertex3f(high.x+pad, high.y+pad, z); GL.glVertex3f(low.x-pad, high.y+pad, z)
        GL.glEnd()
        size = max(self.scene_size * 0.025, 1.5)
        state_color = (1.0, 0.58, 0.18, 1.0) if exposing else (0.24, 0.90, 0.58, 1.0)
        metal = (0.72, 0.79, 0.84, 1.0)
        self._cylinder_z(
            current.x, current.y, current.z + size * 2.25, current.z + size * 4.7,
            size * 0.46, (0.18, 0.24, 0.29, 0.98),
        )
        self._cylinder_z(
            current.x, current.y, current.z + size * 1.75, current.z + size * 2.35,
            size * 0.62, state_color,
        )
        self._cylinder_z(
            current.x, current.y, current.z + size * 0.24, current.z + size * 1.82,
            size * 0.095, metal, 14,
        )
        self._cone_tip(
            current.x, current.y, current.z + size * 0.24, current.z,
            size * 0.095, metal, 14,
        )
        GL.glPointSize(14.0 if exposing else 10.0)
        GL.glColor3f(1.0, 0.67, 0.24) if exposing else GL.glColor3f(0.31, 0.91, 0.62)
        GL.glBegin(GL_POINTS); self._vertex(current); GL.glEnd()

    def _draw_history_layers(self) -> None:
        """Draw fully completed lower layers from an OpenGL display-list cache."""
        if not self.model or self.selected_layer <= 1 or self.selected_layer > len(self.model.layers):
            return
        cache_key = (id(self.model), self.selected_layer)
        if self._history_cache_key != cache_key or not self._history_list:
            if self._history_list:
                GL.glDeleteLists(self._history_list, 1)
            self._history_list = GL.glGenLists(1)
            first = self.model.layers[0].first_move
            last = self.model.layers[self.selected_layer - 2].last_move
            GL.glNewList(self._history_list, GL_COMPILE)
            GL.glLineWidth(3.2)
            GL.glColor4f(0.30, 0.94, 0.61, 1.0)
            GL.glBegin(GL_LINE_STRIP)
            self._vertex(self.model.moves[first].start)
            for index in range(first, last + 1):
                self._vertex(self.model.moves[index].end)
            GL.glEnd()
            GL.glEndList()
            self._history_cache_key = cache_key
        GL.glCallList(self._history_list)

    def _prepare_progress_cache(self, first: int, last: int) -> None:
        if not self.model:
            return
        cache_key = (id(self.model), self.selected_layer, first, last)
        if self._progress_cache_key == cache_key and self._progress_list_base:
            return
        if self._progress_list_base:
            GL.glDeleteLists(self._progress_list_base, self._progress_list_count)
        move_count = last - first + 1
        self._progress_list_count = max(1, math.ceil(move_count / self.PROGRESS_CHUNK_SIZE))
        self._progress_list_base = GL.glGenLists(self._progress_list_count)
        for chunk_index in range(self._progress_list_count):
            chunk_first = first + chunk_index * self.PROGRESS_CHUNK_SIZE
            chunk_last = min(last, chunk_first + self.PROGRESS_CHUNK_SIZE - 1)
            GL.glNewList(self._progress_list_base + chunk_index, GL_COMPILE)
            GL.glBegin(GL_LINE_STRIP)
            self._vertex(self.model.moves[chunk_first].start)
            for move_index in range(chunk_first, chunk_last + 1):
                self._vertex(self.model.moves[move_index].end)
            GL.glEnd()
            GL.glEndList()
        self._progress_cache_key = cache_key

    def _draw_completed_path(self, active_index: int, current: Point3) -> None:
        """Draw lower layers plus current-layer progress as continuous green overlays."""
        if not self.model or not self.model.moves:
            return
        if self.selected_layer > 0 and self.selected_layer <= len(self.model.layers):
            layer = self.model.layers[self.selected_layer - 1]
            first, last = layer.first_move, min(layer.last_move, active_index)
        else:
            first, last = 0, active_index
        if last < first:
            return
        layer_last = self.model.layers[self.selected_layer - 1].last_move if self.selected_layer > 0 else len(self.model.moves) - 1
        self._prepare_progress_cache(first, layer_last)
        GL.glDisable(GL_DEPTH_TEST)
        GL.glLineWidth(3.2)
        GL.glColor4f(0.30, 0.94, 0.61, 1.0)
        self._draw_history_layers()
        move_count = layer_last - first + 1
        completed_moves = min(move_count, max(0, active_index - first))
        completed_chunks = completed_moves // self.PROGRESS_CHUNK_SIZE
        for chunk_index in range(completed_chunks):
            GL.glCallList(self._progress_list_base + chunk_index)
        remainder_first = first + completed_chunks * self.PROGRESS_CHUNK_SIZE
        if remainder_first <= layer_last:
            GL.glBegin(GL_LINE_STRIP)
            self._vertex(self.model.moves[remainder_first].start)
            remainder_end = layer_last + 1 if completed_moves >= move_count else active_index
            for index in range(remainder_first, remainder_end):
                self._vertex(self.model.moves[index].end)
            if completed_moves < move_count:
                self._vertex(current)
            GL.glEnd()
        GL.glEnable(GL_DEPTH_TEST)

    def render(self) -> None:
        if not self._initialized or not self._dc or not self._rc or self._rendering:
            return
        self._rendering = True
        started = time.perf_counter()
        try:
            self._render_scene()
        finally:
            self.last_frame_ms = (time.perf_counter() - started) * 1000.0
            self._rendering = False

    def _render_scene(self) -> None:
        GL.wglMakeCurrent(self._dc, self._rc)
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        GL.glViewport(0, 0, width, height)
        GL.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        GL.glMatrixMode(GL_PROJECTION)
        GL.glLoadIdentity()
        near = max(0.05, self.scene_size * 0.001)
        GLU.gluPerspective(42.0, width / height, near, max(1000.0, self.scene_size * 120.0))
        GL.glMatrixMode(GL_MODELVIEW)
        GL.glLoadIdentity()
        GL.glTranslatef(self.pan_x, self.pan_y, -self.camera_distance)
        GL.glRotatef(self.pitch, 1.0, 0.0, 0.0)
        GL.glRotatef(self.yaw, 0.0, 0.0, 1.0)
        GL.glTranslatef(-self.center.x, -self.center.y, -self.center.z)

        if self.model:
            if self.show_grid:
                self._draw_grid()
            self._draw_axes()
            active_index, active_move, current = self._current_move()
            self._draw_static_path()
            self._draw_completed_path(active_index, current)
            if self.show_machine:
                self._draw_machine(current, bool(active_move and active_move.dwell and active_move.light > 0))
        self._draw_orientation_gizmo(width, height)
        swap_started = time.perf_counter()
        GDI.SwapBuffers(self._dc)
        self.last_swap_ms = (time.perf_counter() - swap_started) * 1000.0

    def framebuffer_probe(self) -> tuple[int, int]:
        """Return sampled non-background pixels and sample count from the front buffer."""
        if not self._initialized or not self._dc or not self._rc:
            return 0, 0
        self.render()
        GL.wglMakeCurrent(self._dc, self._rc)
        GL.glFinish()
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        buffer = (ctypes.c_ubyte * (width * height * 3))()
        GL.glReadBuffer(GL_FRONT)
        GL.glPixelStorei(GL_PACK_ALIGNMENT, 1)
        GL.glReadPixels(0, 0, width, height, GL_RGB, GL_UNSIGNED_BYTE, buffer)
        stride = max(1, min(width, height) // 80)
        different = total = 0
        background = (9, 14, 19)
        for y in range(0, height, stride):
            for x in range(0, width, stride):
                offset = (y * width + x) * 3
                pixel = (buffer[offset], buffer[offset + 1], buffer[offset + 2])
                total += 1
                if sum(abs(pixel[index] - background[index]) for index in range(3)) > 18:
                    different += 1
        return different, total


class ViewerApp:
    # A 75 Hz timer leaves headroom for Tk/Windows event-loop jitter while the
    # display naturally presents at its own refresh rate.
    FRAME_INTERVAL = 1.0 / 75.0
    TIMELINE_INTERVAL = 1.0 / 30.0
    TELEMETRY_INTERVAL = 1.0 / 15.0
    BG = "#0b1016"
    PANEL = "#121a23"
    PANEL_2 = "#17222d"
    BORDER = "#293845"
    TEXT = "#eef4f8"
    MUTED = "#8fa1ae"
    GREEN = "#51e39a"
    CYAN = "#61c7ff"
    AMBER = "#ffbd66"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._timer_resolution_active = WINMM.timeBeginPeriod(1) == 0
        self.model: GCodeModel | None = None
        self.playing = False
        self._gc_paused_by_playback = False
        self.last_tick = time.perf_counter()
        self._last_timeline_at = 0.0
        self._last_telemetry_at = 0.0
        self.speed_levels = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0)
        self.speed_index = 5
        self.speed = tk.DoubleVar(value=self.speed_levels[self.speed_index])
        self.speed_label = tk.StringVar(value="10×")
        self.layer_value = tk.IntVar(value=1)
        self.timeline_value = tk.DoubleVar(value=0.0)
        self.show_travel = tk.BooleanVar(value=True)
        self.show_grid = tk.BooleanVar(value=True)
        self.show_machine = tk.BooleanVar(value=True)
        self.current_file = tk.StringVar(value="尚未载入 G-code")
        self.status = tk.StringVar(value="待机")
        self.progress_label = tk.StringVar(value="00:00 / 00:00")
        self.layer_label = tk.StringVar(value="第 — / — 层")
        self.layer_height_label = tk.StringVar(value="Z — mm")
        self.coord_x = tk.StringVar(value="0.000")
        self.coord_y = tk.StringVar(value="0.000")
        self.coord_z = tk.StringVar(value="0.000")
        self.feed_value = tk.StringVar(value="—")
        self.line_value = tk.StringVar(value="—")
        self.command_value = tk.StringVar(value="尚未开始模拟")
        self.summary_moves = tk.StringVar(value="0")
        self.summary_layers = tk.StringVar(value="0")
        self.summary_distance = tk.StringVar(value="0.00 mm")
        self.summary_duration = tk.StringVar(value="00:00")
        self._seek_render_job: str | None = None
        self._layer_render_job: str | None = None
        self._configure_window()
        self._configure_style()
        self._build_ui()
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self.root.after(16, self._tick)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        if self._gc_paused_by_playback:
            gc.enable()
            self._gc_paused_by_playback = False
        if self._timer_resolution_active:
            WINMM.timeEndPeriod(1)
            self._timer_resolution_active = False

    def _set_playing(self, playing: bool) -> None:
        self.playing = playing
        if playing and gc.isenabled():
            gc.disable()
            self._gc_paused_by_playback = True
        elif not playing and self._gc_paused_by_playback:
            gc.enable()
            self._gc_paused_by_playback = False

    def _configure_window(self) -> None:
        self.root.title("VECTOR 3D · G-code OpenGL Toolpath Viewer")
        self.root.geometry("1700x1000")
        self.root.minsize(1280, 800)
        self.root.configure(bg=self.BG)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass

    def _configure_style(self) -> None:
        for font_name, size in (("TkDefaultFont", 14), ("TkTextFont", 14), ("TkMenuFont", 13)):
            try:
                tkfont.nametofont(font_name).configure(family="Segoe UI", size=size)
            except tk.TclError:
                pass
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 14))
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 14))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 13))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 22))
        style.configure("Section.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 13))
        style.configure("Value.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Consolas", 16, "bold"))
        style.configure("Accent.TLabel", background=self.PANEL, foreground=self.GREEN, font=("Consolas", 16, "bold"))
        style.configure("TButton", background=self.PANEL_2, foreground=self.TEXT, bordercolor=self.BORDER, padding=(14, 11), font=("Segoe UI", 13))
        style.map("TButton", background=[("active", "#20303b"), ("pressed", "#263944")], foreground=[("disabled", "#5d707d")])
        style.configure("Accent.TButton", background=self.GREEN, foreground="#082016", bordercolor=self.GREEN, padding=(17, 12), font=("Segoe UI Semibold", 14))
        style.map("Accent.TButton", background=[("active", "#79efb5"), ("pressed", "#3fcf86")])
        style.configure("Tool.TButton", padding=(11, 9), font=("Segoe UI", 13))
        style.configure("Rail.TButton", padding=(9, 8), font=("Segoe UI Semibold", 15))
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 13))
        style.map("TCheckbutton", background=[("active", self.PANEL)], indicatorcolor=[("selected", self.GREEN), ("!selected", self.PANEL_2)])
        style.configure("TCombobox", fieldbackground=self.PANEL_2, background=self.PANEL_2, foreground=self.TEXT, arrowcolor=self.TEXT)
        style.configure("Horizontal.TScale", background=self.BG, troughcolor="#22313c", bordercolor=self.BG, lightcolor=self.BG, darkcolor=self.BG)

    def _panel(self, parent: tk.Misc, title: str) -> tuple[ttk.Frame, ttk.Frame]:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=0)
        header = ttk.Frame(panel, style="Panel.TFrame", padding=(12, 8, 12, 8))
        header.pack(fill="x")
        ttk.Label(header, text=title.upper(), style="Section.TLabel").pack(side="left")
        ttk.Separator(panel, orient="horizontal").pack(fill="x")
        body = ttk.Frame(panel, style="Panel.TFrame", padding=10)
        body.pack(fill="both", expand=True)
        return panel, body

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(24, 16))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        logo = tk.Label(header, text="V/", width=3, height=1, bg="#dffff0", fg="#082016", font=("Consolas", 22, "bold"), pady=5)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(header, text="VECTOR 3D", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="OPENGL TOOLPATH WORKSTATION", foreground=self.MUTED, background=self.BG, font=("Segoe UI", 12)).grid(row=1, column=1, sticky="w")
        ttk.Label(header, textvariable=self.current_file, foreground=self.MUTED, background=self.BG).grid(row=0, column=2, rowspan=2, padx=18)
        ttk.Button(header, text="打开 G-code", style="Accent.TButton", command=self.open_file).grid(row=0, column=3, rowspan=2)

        main = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        left = ttk.Frame(main, width=300)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_propagate(False)
        file_panel, file_body = self._panel(left, "文件")
        file_panel.pack(fill="x", pady=(0, 10))
        ttk.Button(file_body, text="选择切片文件…", command=self.open_file).pack(fill="x")
        self.file_detail = ttk.Label(file_body, text="支持 .gcode / .gc / .nc / .txt\n文件只在本机解析", style="Muted.TLabel", wraplength=250, justify="left")
        self.file_detail.pack(fill="x", pady=(10, 0))

        view_panel, view_body = self._panel(left, "视图")
        view_panel.pack(fill="x", pady=(0, 10))
        grid = ttk.Frame(view_body, style="Panel.TFrame")
        grid.pack(fill="x")
        for index, (label, key) in enumerate((("等轴测", "iso"), ("俯视", "top"), ("正视", "front"), ("侧视", "side"))):
            ttk.Button(grid, text=label, style="Tool.TButton", command=lambda mode=key: self.viewport.set_preset(mode)).grid(row=index//2, column=index%2, sticky="ew", padx=(0 if index%2==0 else 4, 4 if index%2==0 else 0), pady=2)
        grid.columnconfigure((0, 1), weight=1)
        ttk.Button(view_body, text="适应窗口", command=self.viewport_fit_later).pack(fill="x", pady=(8, 0))

        display_panel, display_body = self._panel(left, "显示")
        display_panel.pack(fill="x", pady=(0, 10))
        for text, variable in (("空移路径", self.show_travel), ("平台网格", self.show_grid), ("模拟机头", self.show_machine)):
            ttk.Checkbutton(display_body, text=text, variable=variable, command=self._apply_display).pack(anchor="w", pady=3)

        interaction_panel, interaction_body = self._panel(left, "操作")
        interaction_panel.pack(fill="x")
        ttk.Label(interaction_body, text="左侧滑轨切换层\n下方滑轨查看当前层走刀", style="Muted.TLabel", justify="left").pack(anchor="w")

        viewport_area = ttk.Frame(main)
        viewport_area.grid(row=0, column=1, sticky="nsew")
        viewport_area.columnconfigure(0, weight=1)
        viewport_area.rowconfigure(0, weight=1)
        scene_row = ttk.Frame(viewport_area)
        scene_row.grid(row=0, column=0, sticky="nsew")
        scene_row.columnconfigure(1, weight=1)
        scene_row.rowconfigure(0, weight=1)

        layer_rail = tk.Frame(scene_row, width=116, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        layer_rail.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        layer_rail.grid_propagate(False)
        tk.Label(layer_rail, text="层切换", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 14), pady=12).pack(fill="x")
        ttk.Button(layer_rail, text="▲", style="Rail.TButton", command=lambda: self.change_layer(1)).pack(fill="x", padx=12, pady=(2, 7))
        tk.Label(layer_rail, textvariable=self.layer_label, bg=self.PANEL, fg=self.GREEN, font=("Consolas", 13, "bold"), wraplength=96, pady=4).pack(fill="x")
        tk.Label(layer_rail, textvariable=self.layer_height_label, bg=self.PANEL, fg=self.MUTED, font=("Consolas", 11), wraplength=96).pack(fill="x", pady=(0, 5))
        self.layer_scale = tk.Scale(
            layer_rail, from_=1, to=1, orient="vertical", variable=self.layer_value, command=self._set_layer,
            showvalue=False, resolution=1, bg=self.PANEL, fg=self.TEXT, troughcolor="#263945",
            activebackground=self.GREEN, highlightthickness=0, bd=0, sliderlength=38, width=16,
        )
        self.layer_scale.pack(fill="both", expand=True, padx=18, pady=4)
        ttk.Button(layer_rail, text="▼", style="Rail.TButton", command=lambda: self.change_layer(-1)).pack(fill="x", padx=12, pady=(7, 12))

        viewport_holder = tk.Frame(scene_row, bg="#090e13", highlightbackground=self.BORDER, highlightthickness=1)
        viewport_holder.grid(row=0, column=1, sticky="nsew")
        viewport_holder.rowconfigure(0, weight=1)
        viewport_holder.columnconfigure(0, weight=1)
        self.viewport = OpenGLViewport(viewport_holder)
        self.viewport.grid(row=0, column=0, sticky="nsew")
        overlay = tk.Label(viewport_holder, text="左键旋转  ·  右键平移  ·  滚轮缩放  ·  双击适应", bg="#101820", fg="#c1ced4", font=("Segoe UI", 13), padx=14, pady=9)
        overlay.place(relx=0.5, y=12, anchor="n")

        axis_key = tk.Frame(viewport_holder, bg="#101820", padx=9, pady=5)
        axis_key.place(x=18, rely=1.0, y=-150, anchor="sw")
        for label, color in (("X", "#ff5964"), ("Y", "#47ef8c"), ("Z", "#4dadff")):
            tk.Label(axis_key, text=label, bg="#101820", fg=color, font=("Consolas", 14, "bold"), padx=6).pack(side="left")

        progress_panel = tk.Frame(viewport_area, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, padx=14, pady=11)
        progress_panel.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        progress_panel.columnconfigure(4, weight=1)
        tk.Label(progress_panel, text="当前层喷头走刀", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 14)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 7))
        tk.Label(progress_panel, textvariable=self.status, bg=self.PANEL, fg=self.GREEN, font=("Segoe UI Semibold", 13)).grid(row=0, column=3, sticky="w", padx=(16, 0), pady=(0, 7))
        tk.Label(progress_panel, textvariable=self.progress_label, bg=self.PANEL, fg=self.MUTED, font=("Consolas", 13)).grid(row=0, column=4, columnspan=4, sticky="e", pady=(0, 7))
        self.reset_button = ttk.Button(progress_panel, text="|◀", width=4, command=self.reset)
        self.reset_button.grid(row=1, column=0, padx=(0, 7))
        self.play_button = ttk.Button(progress_panel, text="▶", style="Accent.TButton", width=4, command=self.toggle_play)
        self.play_button.grid(row=1, column=1, padx=(0, 7))
        ttk.Button(progress_panel, text="▶|", width=4, command=self.step_forward).grid(row=1, column=2, padx=(0, 10))
        self.timeline = tk.Scale(
            progress_panel, from_=0, to=10000, orient="horizontal", command=self._seek, showvalue=False,
            variable=self.timeline_value,
            resolution=1, bg=self.PANEL, troughcolor="#263945", activebackground=self.GREEN,
            highlightthickness=0, bd=0, sliderlength=34, width=16,
        )
        self.timeline.grid(row=1, column=3, columnspan=2, sticky="ew", padx=(0, 14))
        self.timeline.bind("<ButtonPress-1>", self._begin_scrub)
        self.timeline.bind("<ButtonRelease-1>", self._end_scrub)
        ttk.Button(progress_panel, text="−", style="Rail.TButton", width=3, command=lambda: self.change_speed(-1)).grid(row=1, column=5, padx=(0, 5))
        tk.Label(progress_panel, textvariable=self.speed_label, width=6, bg="#0d151d", fg=self.AMBER, font=("Consolas", 14, "bold"), pady=8).grid(row=1, column=6)
        ttk.Button(progress_panel, text="+", style="Rail.TButton", width=3, command=lambda: self.change_speed(1)).grid(row=1, column=7, padx=(5, 0))

        right = ttk.Frame(main, width=368)
        right.grid(row=0, column=2, sticky="nse", padx=(10, 0))
        right.grid_propagate(False)
        stats_panel, stats_body = self._panel(right, "作业概览")
        stats_panel.pack(fill="x", pady=(0, 10))
        self._stat_row(stats_body, "走刀段", self.summary_moves)
        self._stat_row(stats_body, "层数", self.summary_layers)
        self._stat_row(stats_body, "总路径", self.summary_distance)
        self._stat_row(stats_body, "预计时长", self.summary_duration, last=True)

        pos_panel, pos_body = self._panel(right, "实时坐标")
        pos_panel.pack(fill="x", pady=(0, 10))
        for label, variable, color in (("X", self.coord_x, "#ff6b75"), ("Y", self.coord_y, self.GREEN), ("Z", self.coord_z, self.CYAN)):
            row = ttk.Frame(pos_body, style="Panel.TFrame")
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, width=3, bg=self.PANEL_2, fg=color, font=("Consolas", 15, "bold"), padx=6, pady=7).pack(side="left")
            ttk.Label(row, textvariable=variable, style="Value.TLabel").pack(side="right")
        ttk.Separator(pos_body).pack(fill="x", pady=8)
        self._stat_row(pos_body, "进给 mm/min", self.feed_value)
        self._stat_row(pos_body, "源文件行", self.line_value, last=True)

        cmd_panel, cmd_body = self._panel(right, "当前指令")
        cmd_panel.pack(fill="both", expand=True)
        self.command_label = tk.Label(cmd_body, textvariable=self.command_value, bg="#0b1117", fg="#d8e2e7", font=("Consolas", 13), justify="left", anchor="nw", wraplength=315, padx=14, pady=14)
        self.command_label.pack(fill="both", expand=True)

        self.root.bind("<Control-o>", lambda _event: self.open_file())
        self.root.bind("<space>", lambda _event: self.toggle_play())
        self.root.bind("<Left>", lambda _event: self.step_backward())
        self.root.bind("<Right>", lambda _event: self.step_forward())
        self.root.bind("<Up>", lambda _event: self.change_layer(1))
        self.root.bind("<Down>", lambda _event: self.change_layer(-1))
        self.root.bind("<Home>", lambda _event: self.reset())

    def _stat_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, last: bool = False) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, style="Muted.TLabel").pack(side="left")
        ttk.Label(row, textvariable=variable, style="Value.TLabel").pack(side="right")
        if not last:
            ttk.Separator(parent).pack(fill="x", pady=2)

    @staticmethod
    def _format_time(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

    def viewport_fit_later(self) -> None:
        self.viewport.fit_view()

    def _layer_bounds(self) -> tuple[float, float, int, int]:
        if not self.model or not self.model.moves:
            return 0.0, 0.0, 0, 0
        if not self.model.layers:
            return 0.0, self.model.total_duration, 0, len(self.model.moves) - 1
        layer_index = max(1, min(len(self.model.layers), self.layer_value.get())) - 1
        layer = self.model.layers[layer_index]
        first = max(0, min(len(self.model.moves) - 1, layer.first_move))
        last = max(first, min(len(self.model.moves) - 1, layer.last_move))
        return self.model.moves[first].time_start, self.model.moves[last].time_end, first, last

    def _set_timeline_value(self, value: float) -> None:
        # Updating the Tk variable does not invoke Scale's user command. Calling
        # Scale.set() does, and its deferred callback used to pause playback.
        self.timeline_value.set(max(0.0, min(10000.0, value)))

    def _begin_scrub(self, _event: tk.Event | None = None) -> None:
        if not self.model:
            return
        self._set_playing(False)
        self.play_button.configure(text="▶")
        self.status.set("拖动预览")

    def _end_scrub(self, _event: tk.Event | None = None) -> None:
        if self._seek_render_job is not None:
            self.root.after_cancel(self._seek_render_job)
            self._seek_render_job = None
        self._render_seek_frame()

    def _queue_seek_render(self) -> None:
        if self._seek_render_job is None:
            self._seek_render_job = self.root.after(16, self._render_seek_frame)

    def _render_seek_frame(self) -> None:
        self._seek_render_job = None
        self.viewport.render()
        now = time.perf_counter()
        if now - self._last_telemetry_at >= self.TELEMETRY_INTERVAL:
            self._refresh_telemetry()
            self._last_telemetry_at = now

    def _queue_layer_render(self) -> None:
        if self._layer_render_job is None:
            self._layer_render_job = self.root.after(16, self._render_layer_frame)

    def _render_layer_frame(self) -> None:
        self._layer_render_job = None
        self.viewport.invalidate_path_cache()
        self.viewport.render()
        self._refresh_telemetry()

    def change_speed(self, direction: int) -> None:
        self.speed_index = max(0, min(len(self.speed_levels) - 1, self.speed_index + direction))
        value = self.speed_levels[self.speed_index]
        self.speed.set(value)
        self.speed_label.set(f"{value:g}×")
        if self.model:
            self.status.set(f"速度 {value:g}×")

    def change_layer(self, direction: int) -> None:
        if not self.model or not self.model.layers:
            return
        target = max(1, min(len(self.model.layers), self.layer_value.get() + direction))
        if target == self.layer_value.get():
            return
        self.layer_value.set(target)
        self._set_layer(str(target))

    def open_file(self, path: str | None = None) -> None:
        if path is None:
            path = filedialog.askopenfilename(
                title="选择 G-code 文件",
                filetypes=(("G-code", "*.gcode *.gc *.nc *.txt"), ("所有文件", "*.*")),
            )
        if not path:
            return
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8-sig", errors="replace")
            model = parse_gcode(text, source.name)
        except OSError as exc:
            messagebox.showerror("无法读取文件", str(exc), parent=self.root)
            return
        if not model.moves:
            messagebox.showwarning("没有可显示的轨迹", "文件中没有识别到 G0/G1/G2/G3 运动或 G4 停留。", parent=self.root)
            return
        self.model = model
        self._set_playing(False)
        self.viewport.set_model(model)
        self.current_file.set(source.name)
        self.file_detail.configure(text=f"{model.line_count:,} 行 · {source.stat().st_size / 1024:.1f} KB\n{source.parent}")
        self.summary_moves.set(f"{len(model.moves):,}")
        self.summary_layers.set(str(len(model.layers)))
        self.summary_distance.set(f"{model.total_distance:,.2f} mm")
        self.summary_duration.set(self._format_time(model.total_duration))
        layer_count = max(1, len(model.layers))
        self.layer_scale.configure(from_=layer_count, to=1)
        self.layer_value.set(1)
        self._set_timeline_value(0)
        self.status.set("已载入")
        self.play_button.configure(text="▶")
        self._set_layer("1")

    def _apply_display(self) -> None:
        self.viewport.show_travel = self.show_travel.get()
        self.viewport.show_grid = self.show_grid.get()
        self.viewport.show_machine = self.show_machine.get()
        self.viewport.invalidate_path_cache()
        self.viewport.render()

    def _set_layer(self, value: str) -> None:
        if not self.model:
            return
        layer_index = max(1, min(max(1, len(self.model.layers)), int(round(float(value)))))
        self._set_playing(False)
        self.play_button.configure(text="▶")
        self.layer_value.set(layer_index)
        self.viewport.selected_layer = layer_index if self.model.layers else 0
        if self.model.layers:
            layer = self.model.layers[layer_index - 1]
            self.layer_label.set(f"第 {layer_index} / {len(self.model.layers)} 层")
            layer_z = self.model.moves[layer.last_move].end.z
            self.layer_height_label.set(f"Z {layer_z:.3f} mm")
        else:
            self.layer_label.set("第 1 / 1 层")
            self.layer_height_label.set("Z — mm")
        layer_start, _layer_end, _first, _last = self._layer_bounds()
        self.viewport.progress_time = layer_start
        self._set_timeline_value(0)
        self.status.set(f"第 {layer_index} 层")
        self._queue_layer_render()

    def _seek(self, value: str) -> None:
        if not self.model:
            return
        self._begin_scrub()
        layer_start, layer_end, _first, _last = self._layer_bounds()
        duration = max(0.0, layer_end - layer_start)
        self.viewport.progress_time = layer_start + duration * float(value) / 10000.0
        self._queue_seek_render()

    def toggle_play(self) -> None:
        if not self.model:
            self.open_file()
            return
        layer_start, layer_end, _first, _last = self._layer_bounds()
        if self.viewport.progress_time < layer_start or self.viewport.progress_time >= layer_end:
            self.viewport.progress_time = layer_start
            self._set_timeline_value(0)
        self._set_playing(not self.playing)
        self.last_tick = time.perf_counter()
        self.play_button.configure(text="Ⅱ" if self.playing else "▶")
        self.status.set("运行中" if self.playing else "已暂停")

    def reset(self) -> None:
        self._set_playing(False)
        layer_start, _layer_end, _first, _last = self._layer_bounds()
        self.viewport.progress_time = layer_start
        self._set_timeline_value(0)
        self.play_button.configure(text="▶")
        self.status.set("已复位" if self.model else "待机")
        self._refresh_telemetry()
        self.viewport.render()

    def _step(self, direction: int) -> None:
        if not self.model or not self.model.moves:
            return
        self._set_playing(False)
        index, _move, _point = self.viewport._current_move()
        _layer_start, _layer_end, first, last = self._layer_bounds()
        index = max(first, min(last, index + direction))
        self.viewport.progress_time = self.model.moves[index].time_start if direction < 0 else self.model.moves[index].time_end
        layer_start, layer_end, _first, _last = self._layer_bounds()
        self._set_timeline_value(10000 * (self.viewport.progress_time - layer_start) / max(layer_end - layer_start, 1e-9))
        self.play_button.configure(text="▶")
        self.status.set("单步")
        self._refresh_telemetry()
        self.viewport.render()

    def step_forward(self) -> None:
        self._step(1)

    def step_backward(self) -> None:
        self._step(-1)

    def _refresh_telemetry(self) -> None:
        if not self.model:
            return
        _index, move, point = self.viewport._current_move()
        self.coord_x.set(f"{point.x:,.3f} mm")
        self.coord_y.set(f"{point.y:,.3f} mm")
        self.coord_z.set(f"{point.z:,.3f} mm")
        self.feed_value.set(f"{move.feed:,.0f}" if move else "—")
        self.line_value.set(str(move.line_number) if move else "—")
        self.command_value.set(move.source if move else "尚未开始模拟")
        layer_start, layer_end, _first, _last = self._layer_bounds()
        current = max(0.0, min(layer_end - layer_start, self.viewport.progress_time - layer_start))
        self.progress_label.set(f"本层 {self._format_time(current)} / {self._format_time(layer_end - layer_start)}")

    def _tick(self) -> None:
        self.root.after(max(1, int(self.FRAME_INTERVAL * 1000.0)), self._tick)
        now = time.perf_counter()
        if self.playing and self.model:
            layer_start, layer_end, _first, _last = self._layer_bounds()
            elapsed = (now - self.last_tick) * self.speed.get()
            self.viewport.progress_time = min(layer_end, max(layer_start, self.viewport.progress_time + elapsed))
            if now - self._last_timeline_at >= self.TIMELINE_INTERVAL or self.viewport.progress_time >= layer_end:
                self._set_timeline_value(10000 * (self.viewport.progress_time - layer_start) / max(layer_end - layer_start, 1e-9))
                self._last_timeline_at = now
            if now - self._last_telemetry_at >= self.TELEMETRY_INTERVAL or self.viewport.progress_time >= layer_end:
                self._refresh_telemetry()
                self._last_telemetry_at = now
            self.viewport.render()
            if self.viewport.progress_time >= layer_end:
                self._set_playing(False)
                self.play_button.configure(text="▶")
                self.status.set("本层已完成")
        self.last_tick = now


def self_test(path: Path) -> int:
    model = parse_gcode(path.read_text(encoding="utf-8-sig", errors="replace"), path.name)
    dwells = [move.duration for move in model.moves if move.dwell]
    print(
        f"OK {model.name}: lines={model.line_count} moves={len(model.moves)} "
        f"layers={len(model.layers)} distance={model.total_distance:.3f}mm "
        f"duration={model.total_duration:.3f}s dwells={dwells}"
    )
    return 0 if model.moves else 2


def _make_performance_model(move_count: int = 120_000, layer_count: int = 12) -> GCodeModel:
    """Build a deterministic, slicer-sized model without touching the filesystem."""
    model = GCodeModel(name="synthetic-performance.gcode", line_count=move_count)
    per_layer = move_count // layer_count
    position = Point3()
    for layer_index in range(layer_count):
        first = len(model.moves)
        layer_name = str(layer_index)
        for local_index in range(per_layer):
            row, column = divmod(local_index, 200)
            x = column * 0.5 if row % 2 == 0 else (199 - column) * 0.5
            y = (row % 50) * 0.5
            end = Point3(x, y, layer_index * 0.2, position.e + 0.01)
            distance = math.sqrt(
                (end.x - position.x) ** 2 + (end.y - position.y) ** 2 + (end.z - position.z) ** 2
            )
            duration = max(distance / 100.0, 0.0001)
            move = Move(
                position, end, "G1", "G1 synthetic", len(model.moves) + 1, 6000.0,
                duration, distance, layer_name, travel=False,
                time_start=model.total_duration, time_end=model.total_duration + duration,
            )
            model.moves.append(move)
            model.total_duration += duration
            model.total_distance += distance
            model.time_ends.append(model.total_duration)
            position = end
        model.layers.append(Layer(layer_name, first, len(model.moves) - 1))
    model.min_point = Point3(0.0, 0.0, 0.0)
    model.max_point = Point3(99.5, 24.5, (layer_count - 1) * 0.2)
    return model


def performance_test(app: ViewerApp, root: tk.Tk) -> int:
    model = _make_performance_model()
    app.model = model
    app.viewport.set_model(model)
    app.layer_scale.configure(from_=len(model.layers), to=1)
    results = []
    for smooth_lines in (True, False):
        (GL.glEnable if smooth_lines else GL.glDisable)(GL_LINE_SMOOTH)
        for layer_number in (1, len(model.layers)):
            app.layer_value.set(layer_number)
            app.viewport.selected_layer = layer_number
            app.viewport.invalidate_path_cache()
            layer = model.layers[layer_number - 1]
            layer_start = model.moves[layer.first_move].time_start
            layer_end = model.moves[layer.last_move].time_end
            app.viewport.progress_time = layer_start + (layer_end - layer_start) * 0.5
            app.viewport.render()
            cold_ms = app.viewport.last_frame_ms
            frames: list[float] = []
            swaps: list[float] = []
            for frame in range(36):
                ratio = 0.1 + 0.8 * frame / 35
                app.viewport.progress_time = layer_start + (layer_end - layer_start) * ratio
                app.viewport.render()
                frames.append(app.viewport.last_frame_ms)
                swaps.append(app.viewport.last_swap_ms)
            ordered = sorted(frames)
            p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
            results.append(
                f"smooth={int(smooth_lines)}/L{layer_number}:cold={cold_ms:.2f}ms "
                f"median={statistics.median(frames):.2f}ms p95={p95:.2f}ms "
                f"swap={statistics.median(swaps):.2f}ms"
            )
    GL.glDisable(GL_LINE_SMOOTH)
    layer_moves = len(model.moves) // len(model.layers)
    print(f"OPENGL_PERF moves={len(model.moves)} layer_moves={layer_moves} " + " ".join(results))
    last_layer = model.layers[-1]
    app.layer_value.set(len(model.layers))
    app.viewport.selected_layer = len(model.layers)
    app.viewport.invalidate_path_cache()
    app.viewport.progress_time = model.moves[last_layer.first_move].time_start
    app.viewport.render()
    render_times: list[float] = []
    render_costs: list[float] = []
    original_render = app.viewport.render

    def measured_render() -> None:
        render_times.append(time.perf_counter())
        original_render()
        render_costs.append(app.viewport.last_frame_ms)

    def finish_pacing_test() -> None:
        app.playing = False
        gc.enable()
        intervals = [(right - left) * 1000.0 for left, right in zip(render_times, render_times[1:])]
        if intervals:
            ordered = sorted(intervals)
            p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
            fps = 1000.0 / statistics.mean(intervals)
            bins = (
                sum(value < 22.0 for value in intervals),
                sum(22.0 <= value < 38.0 for value in intervals),
                sum(value >= 38.0 for value in intervals),
            )
            print(
                f"OPENGL_PACING frames={len(render_times)} fps={fps:.1f} "
                f"interval_median={statistics.median(intervals):.2f}ms interval_p95={p95:.2f}ms "
                f"render_median={statistics.median(render_costs):.2f}ms render_max={max(render_costs):.2f}ms "
                f"bins_lt22_38plus={bins}"
            )
        root.destroy()

    app.viewport.render = measured_render  # type: ignore[method-assign]

    def start_pacing_test() -> None:
        render_times.clear()
        render_costs.clear()
        app.playing = True
        gc.disable()
        app.last_tick = time.perf_counter()
        root.after(1500, finish_pacing_test)

    root.after(400, start_pacing_test)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenGL G-code toolpath viewer")
    parser.add_argument("file", nargs="?", type=Path, help="G-code file to open")
    parser.add_argument("--self-test", action="store_true", help="Parse the file and exit without opening a GUI")
    parser.add_argument("--render-test", action="store_true", help="Render one GUI frame, probe OpenGL pixels, and exit")
    parser.add_argument("--performance-test", action="store_true", help="Benchmark a large in-memory toolpath and exit")
    args = parser.parse_args()
    if args.self_test:
        if not args.file:
            parser.error("--self-test requires a G-code file")
        return self_test(args.file)
    root = tk.Tk()
    app = ViewerApp(root)
    if args.file:
        root.after(120, lambda: app.open_file(str(args.file)))
    if args.render_test:
        if not args.file:
            parser.error("--render-test requires a G-code file")

        def finish_render_test() -> None:
            if not app.model:
                raise RuntimeError("Render test did not load the G-code model")
            if app.model.layers:
                assert app.layer_value.get() == 1
                if len(app.model.layers) > 1:
                    app.change_layer(1)
                    assert app.layer_value.get() == 2
                layer_start, layer_end, first, last = app._layer_bounds()
                app._seek("2500")
                queued_seek = app._seek_render_job
                app._seek("5000")
                assert first <= app.viewport._current_move()[0] <= last
                assert layer_start <= app.viewport.progress_time <= layer_end
                assert not app.playing and queued_seek is not None and app._seek_render_job == queued_seek
                app._end_scrub()
                assert app._seek_render_job is None
            old_speed = app.speed.get()
            app.change_speed(1)
            assert app.speed.get() > old_speed
            app.viewport.progress_time = max(layer_start, layer_end - 0.001)
            app.playing = True
            app.last_tick = time.perf_counter() - 1.0
            app._tick()
            assert not app.playing and app.viewport.progress_time == layer_end
            non_background, samples = app.viewport.framebuffer_probe()
            if len(app.model.layers) > 1:
                assert app.viewport._history_list and app.viewport._history_cache_key == (id(app.model), 2)
            print(
                f"OPENGL_FRAME_OK non_background={non_background}/{samples} "
                f"layer={app.layer_value.get()} speed={app.speed.get():g}x "
                f"history_cached={bool(app.viewport._history_list)}"
            )
            root.destroy()

        root.after(900, finish_render_test)
    elif args.performance_test:
        root.after(350, lambda: performance_test(app, root))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
