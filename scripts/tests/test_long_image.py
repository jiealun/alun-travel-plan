"""Tests for final long-image registration; trip data is synthetic."""

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import register_long_image as image_registry
import render_outputs


def png_bytes(width, height):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = (b"\x00" + b"\x00" * width * 3) * height
    return (image_registry.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class LongImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.trip = json.loads((ROOT / "assets/examples/sample_trip.json").read_text(encoding="utf-8"))
        self.trip["user_confirmed"] = True
        self.trip_path = self.base / "trip.json"
        self.trip_path.write_text(json.dumps(self.trip, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(render_outputs.main([str(self.trip_path)]), 0)
        self.png_path = self.base / "outputs" / f"{self.trip['trip_id']}_v{self.trip['plan_version']}_1x5.png"

    def tearDown(self):
        self.tmp.cleanup()

    def test_registers_exact_ratio_and_hash(self):
        content = png_bytes(10, 50)
        self.png_path.write_bytes(content)
        self.assertEqual(image_registry.main([str(self.trip_path), str(self.png_path)]), 0)
        manifest = json.loads((self.base / "manifest.v1.json").read_text(encoding="utf-8"))
        card = next(entry for entry in manifest["files"] if entry["kind"] == "card")
        self.assertEqual(card["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(card["path"], f"outputs/{self.png_path.name}")

    def test_rejects_wrong_ratio_without_changing_manifest(self):
        self.png_path.write_bytes(png_bytes(10, 49))
        manifest_path = self.base / "manifest.v1.json"
        before = manifest_path.read_bytes()
        self.assertEqual(image_registry.main([str(self.trip_path), str(self.png_path)]), 1)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_rejects_unconfirmed_trip(self):
        self.png_path.write_bytes(png_bytes(10, 50))
        self.trip["user_confirmed"] = False
        self.trip_path.write_text(json.dumps(self.trip, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(image_registry.main([str(self.trip_path), str(self.png_path)]), 1)


if __name__ == "__main__":
    unittest.main()
