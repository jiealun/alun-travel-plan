#!/usr/bin/env python3
"""Verify and register one confirmed 1:5 PNG in a trip's artifact manifest."""

import argparse
import hashlib
import json
import os
import struct
import sys

from trip_support import atomic_json, canonical_hash, inside


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def dimensions(path):
    with open(path, "rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError("文件不是有效的 PNG 图片头")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height != width * 5:
        raise ValueError(f"长图尺寸须为精确 1:5，实际为 {width}×{height}")
    return width, height


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验并登记 Alun旅行规划 1:5 长图")
    parser.add_argument("trip", help="已确认的 trip.json")
    parser.add_argument("png", help="已导出的最终 PNG")
    args = parser.parse_args(argv)

    try:
        trip_path = os.path.realpath(args.trip)
        base = os.path.dirname(trip_path)
        with open(trip_path, encoding="utf-8") as stream:
            trip = json.load(stream)
        if trip.get("user_confirmed") is not True:
            raise ValueError("行程尚未标记为用户已确认")
        expected = f"{trip['trip_id']}_v{trip['plan_version']}_1x5.png"
        image_path = inside(base, args.png)
        if os.path.basename(image_path) != expected:
            raise ValueError(f"终稿文件名须为 {expected}")
        width, height = dimensions(image_path)

        manifest_path = inside(base, os.path.join(base, f"manifest.v{trip['plan_version']}.json"))
        with open(manifest_path, encoding="utf-8") as stream:
            manifest = json.load(stream)
        if (manifest.get("trip_id") != trip["trip_id"]
                or manifest.get("plan_version") != trip["plan_version"]
                or manifest.get("content_hash") != canonical_hash(trip)):
            raise ValueError("产物清单与当前已确认行程版本不一致")
        if sorted(item["kind"] for item in manifest["files"] if item["kind"] in ("md", "html")) != ["html", "md"]:
            raise ValueError("请先完整生成并校验 MD 与 HTML")

        with open(image_path, "rb") as stream:
            image_bytes = stream.read()
        rel = os.path.relpath(image_path, base).replace(os.sep, "/")
        entry = {"path": rel, "kind": "card", "sha256": hashlib.sha256(image_bytes).hexdigest(), "bytes": len(image_bytes)}
        manifest["files"] = [item for item in manifest["files"] if item["kind"] != "card"] + [entry]
        atomic_json(manifest_path, manifest)
        print(f"已登记 1:5 长图：{rel}（{width}×{height}）")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"无法登记长图：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
