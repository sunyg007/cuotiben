# -*- coding: utf-8 -*-
"""
生成 App 图标（PNG）。不依赖 Pillow，用 zlib 手写 PNG。
图形用有符号距离场（SDF）+ 边缘羽化做抗锯齿，每个像素只算一次，快且干净。

用法：python make_icons.py
产出：web/icon-512.png  web/icon-192.png  web/apple-touch-icon.png(180)  web/favicon-32.png
"""
import math
import os
import struct
import zlib

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

C_TOP = (47, 107, 237)      # #2f6bed
C_BOTTOM = (86, 150, 255)   # #5696ff
WHITE = (255, 255, 255)

# 画布逻辑尺寸，所有坐标都按 512 来写，输出时按比例缩放
CANVAS = 512.0


def sd_round_rect(px, py, cx, cy, hw, hh, r):
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax, ay = max(dx, 0.0), max(dy, 0.0)
    return math.hypot(ax, ay) + min(max(dx, dy), 0.0) - r


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def coverage(d, aa):
    """距离 -> 覆盖度，aa 是羽化带宽（逻辑单位）"""
    return min(1.0, max(0.0, 0.5 - d / aa))


def gradient(y):
    t = min(1.0, max(0.0, y / CANVAS))
    return tuple(round(C_TOP[i] + (C_BOTTOM[i] - C_TOP[i]) * t) for i in range(3))


def sample(px, py, aa):
    """返回 (r, g, b)，已做抗锯齿合成"""
    r, g, b = gradient(py)
    bg = (r, g, b, 1.0)

    def over(color, alpha):
        nonlocal r, g, b
        if alpha <= 0:
            return
        r = round(color[0] * alpha + r * (1 - alpha))
        g = round(color[1] * alpha + g * (1 - alpha))
        b = round(color[2] * alpha + b * (1 - alpha))

    # 闪光点（画在机身之前，会被机身盖住的边缘自然融合）
    over(WHITE, coverage(sd_circle(px, py, 352, 214, 14), aa))
    # 顶部取景凸起
    over(WHITE, coverage(sd_round_rect(px, py, 256, 168, 46, 22, 11), aa))
    # 机身
    over(WHITE, coverage(sd_round_rect(px, py, 256, 270, 140, 92, 30), aa))
    # 镜头外圈（用背景色挖出蓝环）
    over(bg[:3], coverage(sd_circle(px, py, 256, 270, 64), aa))
    # 镜头内圈
    over(WHITE, coverage(sd_circle(px, py, 256, 270, 44), aa))
    return r, g, b


def render(size):
    """返回 RGBA 原始像素行"""
    aa = max(1.0, CANVAS / size) * 1.2   # 小尺寸时按设备像素放大羽化带，避免糊
    scale = CANVAS / size
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG 行过滤类型 0
        py = (y + 0.5) * scale
        for x in range(size):
            px = (x + 0.5) * scale
            r, g, b = sample(px, py, aa)
            raw += bytes((r, g, b, 255))
    return bytes(raw)


def write_png(path, size):
    raw = render(size)
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    targets = [("icon-512.png", 512), ("icon-192.png", 192),
               ("apple-touch-icon.png", 180), ("favicon-32.png", 32)]
    for name, size in targets:
        path = os.path.join(OUT_DIR, name)
        n = write_png(path, size)
        print("生成 %-22s %3dx%-4d %6d 字节" % (name, size, size, n))
