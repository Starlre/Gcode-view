import json
import re
import struct
import tempfile
import unittest
from pathlib import Path

from hku_backend import Backend, settings_from_json


class BackendTests(unittest.TestCase):
    def test_real_slice_and_non_overwriting_job_export(self):
        backend = Backend()
        model = backend.handle("demo", {})
        self.assertEqual(model["triangles"], 12)
        result = backend.handle("slice", {"settings": {
            "layer_height": 5, "first_layer_height": 5,
            "fill_enabled": True, "fill_spacing": 1, "support_layers": 1}})
        self.assertEqual(result["layerCount"], 3)
        first_layer = result["gcode"].split(";LAYER_START:0")[1].split(";LAYER_END")[0]
        widths = []
        for ring in first_layer.split("M101;")[1:]:
            coords = [(float(x), float(y)) for x, y in re.findall(r"G1 X([-\d.]+) Y([-\d.]+)", ring.split("M102;")[0])]
            widths.append(max(x for x, y in coords) - min(x for x, y in coords))
        self.assertEqual(widths, sorted(widths))
        self.assertGreater(len(widths), 3)
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "示例方块 · 15 mm"
            original.mkdir()
            sentinel = original / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")
            exported = backend.handle("export", {"directory": folder})
            self.assertNotEqual(exported["directory"], str(original))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")
            job = Path(exported["directory"])
            self.assertEqual(len(list(job.glob("*.png"))), 3)
            header = (job / "1.png").read_bytes()
            self.assertEqual(struct.unpack(">II", header[16:24]), (3840, 2160))
            self.assertIn('M6054 "1.png"', (job / "run.gcode").read_text(encoding="utf-8"))

    def test_invalid_parameters_are_rejected_before_slicing(self):
        for settings in ({"fill_spacing": 0}, {"layer_height": float("nan")},
                         {"support_layers": 1.5}, {"slice_direction": [0, 0, 0]},
                         {"margin": 30, "bed_y": 45}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings_from_json(settings)

    def test_bad_import_preserves_loaded_model(self):
        backend = Backend()
        backend.handle("demo", {})
        mesh = backend.mesh
        with self.assertRaises(Exception):
            backend.handle("load", {"path": "missing_model.stl"})
        self.assertIs(backend.mesh, mesh)


if __name__ == "__main__":
    unittest.main()
