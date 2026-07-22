"""Dependency-free PNG plots for post-run scientific analysis.

The station's configured Gamry Python is intentionally small and does not ship
with Matplotlib.  This module keeps analysis deployable there by providing the
few raster primitives needed for the Levich result plots.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Any, Iterable


Color = tuple[int, int, int]


FONT = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "=": ("00000", "11111", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
}


class Canvas:
    def __init__(self, width: int, height: int, background: Color = (255, 255, 255)) -> None:
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(background * (self.width * self.height))

    def pixel(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def fill_rect(self, left: int, top: int, right: int, bottom: int, color: Color) -> None:
        left = max(0, min(self.width, int(left)))
        right = max(0, min(self.width, int(right)))
        top = max(0, min(self.height, int(top)))
        bottom = max(0, min(self.height, int(bottom)))
        if right <= left or bottom <= top:
            return
        row = bytes(color) * (right - left)
        for y in range(top, bottom):
            offset = (y * self.width + left) * 3
            self.pixels[offset : offset + len(row)] = row

    def line(self, x0: int, y0: int, x1: int, y1: int, color: Color, width: int = 1) -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        radius = max(0, int(width) // 2)
        while True:
            self.fill_rect(x0 - radius, y0 - radius, x0 + radius + 1, y0 + radius + 1, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    def circle(self, cx: int, cy: int, radius: int, color: Color) -> None:
        radius = max(1, int(radius))
        for y in range(-radius, radius + 1):
            half = int(math.sqrt(max(0, radius * radius - y * y)))
            self.fill_rect(cx - half, cy + y, cx + half + 1, cy + y + 1, color)

    def text(self, x: int, y: int, value: Any, color: Color = (35, 74, 104), scale: int = 2) -> None:
        cursor = int(x)
        scale = max(1, int(scale))
        for character in str(value).upper():
            glyph = FONT.get(character, FONT[" "])
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.fill_rect(
                            cursor + column_index * scale,
                            int(y) + row_index * scale,
                            cursor + (column_index + 1) * scale,
                            int(y) + (row_index + 1) * scale,
                            color,
                        )
            cursor += 6 * scale

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = bytearray()
        row_bytes = self.width * 3
        for y in range(self.height):
            raw.append(0)
            start = y * row_bytes
            raw.extend(self.pixels[start : start + row_bytes])

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        payload = bytearray(b"\x89PNG\r\n\x1a\n")
        payload.extend(chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)))
        payload.extend(chunk(b"IDAT", zlib.compress(bytes(raw), level=6)))
        payload.extend(chunk(b"IEND", b""))
        path.write_bytes(payload)
        return path


def finite_pairs(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [
        (float(x), float(y))
        for x, y in points
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]


def padded_range(values: list[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    span = high - low
    padding = span * 0.08 if span else max(abs(low) * 0.08, 1.0)
    return low - padding, high + padding


def format_tick(value: float) -> str:
    magnitude = abs(value)
    if magnitude and (magnitude < 0.001 or magnitude >= 10000):
        return f"{value:.2E}"
    return f"{value:.4G}"


def render_xy_plot(
    path: str | Path,
    *,
    title: str,
    x_label: str,
    y_label: str,
    points: Iterable[tuple[float, float]],
    fit_points: Iterable[tuple[float, float]] | None = None,
    bands: list[dict[str, Any]] | None = None,
    footer: str = "RPM SOURCE: COMMANDED | STABILIZATION MODE: FIXED DELAY",
) -> Path:
    data = finite_pairs(points)
    if not data:
        raise ValueError("plot needs at least one finite point")
    fit = finite_pairs(fit_points or [])
    width, height = 1000, 650
    canvas = Canvas(width, height)
    margin = {"left": 100, "right": 35, "top": 75, "bottom": 95}
    plot_left, plot_top = margin["left"], margin["top"]
    plot_right, plot_bottom = width - margin["right"], height - margin["bottom"]
    xmin, xmax = padded_range([point[0] for point in data] + [point[0] for point in fit])
    ymin, ymax = padded_range([point[1] for point in data] + [point[1] for point in fit])

    def sx(value: float) -> int:
        return int(plot_left + (value - xmin) * (plot_right - plot_left) / (xmax - xmin))

    def sy(value: float) -> int:
        return int(plot_bottom - (value - ymin) * (plot_bottom - plot_top) / (ymax - ymin))

    band_colors = [(235, 246, 252), (245, 239, 250)]
    for index, band in enumerate(bands or []):
        start = float(band["start"])
        end = float(band["end"])
        canvas.fill_rect(sx(start), plot_top, sx(end), plot_bottom, band_colors[index % 2])
        canvas.line(sx(start), plot_top, sx(start), plot_bottom, (130, 161, 181))
        label = str(band.get("label", ""))
        if label:
            canvas.text(max(plot_left, sx(start) + 4), plot_top + 6, label, (70, 94, 112), scale=1)

    for tick in range(6):
        fraction = tick / 5
        x = int(plot_left + fraction * (plot_right - plot_left))
        y = int(plot_bottom - fraction * (plot_bottom - plot_top))
        canvas.line(x, plot_top, x, plot_bottom, (220, 231, 238))
        canvas.line(plot_left, y, plot_right, y, (220, 231, 238))
        canvas.text(x - 25, plot_bottom + 14, format_tick(xmin + fraction * (xmax - xmin)), scale=1)
        canvas.text(8, y - 4, format_tick(ymin + fraction * (ymax - ymin)), scale=1)

    canvas.line(plot_left, plot_top, plot_left, plot_bottom, (36, 78, 105), width=2)
    canvas.line(plot_left, plot_bottom, plot_right, plot_bottom, (36, 78, 105), width=2)
    canvas.text(plot_left, 22, title, (16, 54, 79), scale=2)
    canvas.text(plot_left, height - 34, footer, (75, 99, 115), scale=1)
    canvas.text((plot_left + plot_right) // 2 - len(x_label) * 6, height - 68, x_label, scale=2)
    canvas.text(8, 50, y_label, scale=1)

    previous: tuple[int, int] | None = None
    for x_value, y_value in data:
        current = (sx(x_value), sy(y_value))
        if previous is not None:
            canvas.line(previous[0], previous[1], current[0], current[1], (47, 134, 193), width=2)
        previous = current
    if len(data) <= 100:
        for x_value, y_value in data:
            canvas.circle(sx(x_value), sy(y_value), 4, (35, 105, 151))

    if fit:
        ordered = sorted(fit)
        for first, second in zip(ordered, ordered[1:]):
            canvas.line(sx(first[0]), sy(first[1]), sx(second[0]), sy(second[1]), (190, 53, 53), width=3)

    return canvas.save(path)
