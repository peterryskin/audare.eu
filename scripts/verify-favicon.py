import argparse
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "favicon.svg"
PNG_PATH = ROOT / "favicon-16.png"
SIZE = 16
PIXEL_CENTER = (SIZE - 1) / 2
BACKGROUND = (250, 249, 247, 255)
SVG_NS = "{http://www.w3.org/2000/svg}"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def parse_color(value):
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if not match:
        raise AssertionError(f"Expected a six-digit hex color, got {value!r}")
    hex_value = match.group(1)
    return tuple(int(hex_value[index:index + 2], 16) for index in (0, 2, 4))


def verify_geometry():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    if 'href="/favicon.svg"' not in html or 'href="/favicon-16.png"' not in html:
        raise AssertionError("index.html must reference both favicon formats")

    root = ET.parse(SVG_PATH).getroot()
    view_box = tuple(float(value) for value in root.attrib["viewBox"].split())
    if view_box != (-0.5, -0.5, 16.0, 16.0):
        raise AssertionError(f"Unexpected SVG viewBox: {view_box}")

    circles = root.findall(f"{SVG_NS}circle")
    if len(circles) != 1:
        raise AssertionError(f"Expected one circle, found {len(circles)}")
    circle = circles[0]
    cx = float(circle.attrib["cx"])
    cy = float(circle.attrib["cy"])
    radius = float(circle.attrib["r"])
    if (cx, cy) != (PIXEL_CENTER, PIXEL_CENTER):
        raise AssertionError(f"Circle center is {(cx, cy)}, expected {(PIXEL_CENTER,) * 2}")

    min_x, min_y, width, height = view_box
    max_x = min_x + width
    max_y = min_y + height
    margins = (
        cx - radius - min_x,
        max_x - cx - radius,
        cy - radius - min_y,
        max_y - cy - radius,
    )
    if len(set(margins)) != 1 or margins[0] <= 0:
        raise AssertionError(f"Circle margins are not equal and positive: {margins}")

    masks = root.findall(f".//{SVG_NS}mask")
    paths = [path for mask in masks for path in mask.findall(f"{SVG_NS}path")]
    if len(paths) != 1:
        raise AssertionError(f"Expected one cut path, found {len(paths)}")
    path = paths[0]
    stroke_width = float(path.attrib["stroke-width"])
    if stroke_width != 2:
        raise AssertionError(f"Cut width is {stroke_width}, expected 2")
    coordinates = tuple(float(value) for value in re.findall(
        r"-?\d+(?:\.\d+)?", path.attrib["d"]
    ))
    if len(coordinates) != 4:
        raise AssertionError(f"Expected two cut endpoints, got {coordinates}")
    x1, y1, x2, y2 = coordinates
    dx = x2 - x1
    dy = y2 - y1
    midpoint = ((x1 + x2) / 2, (y1 + y2) / 2)
    if midpoint != (cx, cy):
        raise AssertionError(f"Cut midpoint is {midpoint}, expected {(cx, cy)}")
    if not (dx > 0 and dy < 0 and abs(dx) == abs(dy)):
        raise AssertionError(f"Cut is not a bottom-left to top-right 45-degree diagonal: {(dx, dy)}")
    if (x1 + y1) != (cx + cy) or (x2 + y2) != (cx + cy):
        raise AssertionError("Cut endpoints do not lie on the center diagonal")
    if math.hypot(dx, dy) <= 2 * radius:
        raise AssertionError("Cut path does not extend across the full circle")

    return cx, cy, radius, stroke_width, cx + cy, parse_color(circle.attrib["fill"]), margins


def render_icon(geometry):
    cx, cy, radius, stroke_width, line_sum, color, _ = geometry
    samples_per_axis = 32
    pixels = []
    for pixel_y in range(SIZE):
        for pixel_x in range(SIZE):
            coverage = 0
            for sample_y in range(samples_per_axis):
                y = pixel_y - 0.5 + (sample_y + 0.5) / samples_per_axis
                for sample_x in range(samples_per_axis):
                    x = pixel_x - 0.5 + (sample_x + 0.5) / samples_per_axis
                    in_circle = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
                    in_cut = abs(x + y - line_sum) <= stroke_width / math.sqrt(2)
                    if in_circle and not in_cut:
                        coverage += 1
            alpha = round(255 * coverage / samples_per_axis ** 2)
            pixels.append((*color, alpha) if alpha else (0, 0, 0, 0))
    return pixels


def encode_png(width, height, pixels):
    if len(pixels) != width * height:
        raise AssertionError("PNG pixel count does not match its dimensions")
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for pixel in pixels[y * width:(y + 1) * width]:
            raw.extend(pixel)

    def chunk(name, data):
        checksum = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def write_png(path, width, height, pixels):
    path.write_bytes(encode_png(width, height, pixels))


def read_rgba_png(path):
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise AssertionError(f"{path} is not a PNG")
    offset = len(PNG_SIGNATURE)
    compressed = bytearray()
    width = height = None
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        name = data[offset + 4:offset + 8]
        contents = data[offset + 8:offset + 8 + length]
        checksum = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        if zlib.crc32(name + contents) & 0xFFFFFFFF != checksum:
            raise AssertionError(f"PNG chunk checksum failed in {path}")
        offset += 12 + length
        if name == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", contents)
            )
            if (bit_depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise AssertionError(f"Unsupported PNG format in {path}")
        elif name == b"IDAT":
            compressed.extend(contents)
        elif name == b"IEND":
            break
    if width is None or height is None:
        raise AssertionError(f"PNG dimensions missing in {path}")

    raw = zlib.decompress(compressed)
    stride = width * 4
    pixels = []
    for y in range(height):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        if len(row) != stride + 1 or row[0] != 0:
            raise AssertionError(f"Unsupported PNG row encoding in {path}")
        pixels.extend(
            tuple(row[index:index + 4])
            for index in range(1, len(row), 4)
        )
    return width, height, pixels


def verify_pixels(pixels):
    for y in range(SIZE):
        for x in range(SIZE):
            if pixels[y * SIZE + x] != pixels[(SIZE - 1 - y) * SIZE + SIZE - 1 - x]:
                raise AssertionError(f"180-degree pixel symmetry fails at {(x, y)}")

    opaque = [
        (x, y)
        for y in range(SIZE)
        for x in range(SIZE)
        if pixels[y * SIZE + x][3] > 0
    ]
    bounds = (
        min(x for x, _ in opaque),
        min(y for _, y in opaque),
        max(x for x, _ in opaque) + 1,
        max(y for _, y in opaque) + 1,
    )
    if bounds != (1, 1, 15, 15):
        raise AssertionError(f"Raster bounds are {bounds}, expected one-pixel equal margins")

    upper_side = sum(
        pixels[y * SIZE + x][3]
        for y in range(SIZE)
        for x in range(SIZE)
        if x + y < 15
    )
    lower_side = sum(
        pixels[y * SIZE + x][3]
        for y in range(SIZE)
        for x in range(SIZE)
        if x + y > 15
    )
    if upper_side != lower_side:
        raise AssertionError(f"Half weights differ: {upper_side} != {lower_side}")

    return upper_side, lower_side


def write_preview(image, destination):
    background = []
    for red, green, blue, alpha in image:
        background.append(tuple(
            round(channel * alpha / 255 + BACKGROUND[index] * (255 - alpha) / 255)
            for index, channel in enumerate((red, green, blue))
        ) + (255,))
    preview = []
    for y in range(SIZE):
        for _ in range(16):
            row = []
            for x in range(SIZE):
                row.extend([background[y * SIZE + x]] * 16)
            preview.extend(row)

    write_png(destination, SIZE * 16, SIZE * 16, preview)
    width, height, output = read_rgba_png(destination)
    if (width, height) != (SIZE * 16, SIZE * 16):
        raise AssertionError(f"Unexpected preview size: {(width, height)}")
    for y in range(SIZE):
        for x in range(SIZE):
            expected = background[y * SIZE + x]
            for preview_y in range(y * 16, (y + 1) * 16):
                for preview_x in range(x * 16, (x + 1) * 16):
                    if output[preview_y * width + preview_x] != expected:
                        raise AssertionError("Preview is not nearest-neighbour enlarged")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    geometry = verify_geometry()
    pixels = render_icon(geometry)
    side_weights = verify_pixels(pixels)
    write_png(PNG_PATH, SIZE, SIZE, pixels)
    width, height, saved_pixels = read_rgba_png(PNG_PATH)
    if (width, height) != (SIZE, SIZE) or saved_pixels != pixels:
        raise AssertionError("Saved favicon PNG differs from the verified raster")
    verify_pixels(saved_pixels)
    if args.preview:
        write_preview(pixels, args.preview)

    cx, cy, radius, stroke_width, _, _, margins = geometry
    print(f"Circle center: ({cx:g}, {cy:g})")
    print(f"Circle radius: {radius:g}; equal margins: {margins[0]:g}px")
    print(f"Diagonal cut: {stroke_width:g}px at exactly 45 degrees through center")
    print(f"Half alpha weights: {side_weights[0]} = {side_weights[1]}")
    print("16x16 RGBA raster has exact 180-degree symmetry.")
    print(f"Native icon: {PNG_PATH}")
    if args.preview:
        print(f"Nearest-neighbour preview: {args.preview}")


if __name__ == "__main__":
    main()
