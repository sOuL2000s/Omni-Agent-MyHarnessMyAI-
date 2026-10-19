# scripts/make_icon.py
"""Generate assets/omni.ico — a multi-resolution Windows icon in the
style of the web UI's brand mark (rounded indigo square + white ring).

Run:  python scripts/make_icon.py
Output: assets/omni.ico  (16, 24, 32, 48, 64, 128, 256 px)

Stdlib only; no Pillow required. Each size is rendered supersampled
(4x) and box-downsampled for anti-aliasing.
"""
import struct
import zlib
from math import hypot
from pathlib import Path

BG      = (79, 70, 229)     # --accent #4f46e5
BG_DARK = (99, 102, 241)    # --accent-2 #6366f1 (subtle gradient)
INK     = (255, 255, 255)

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                      # supersample factor


def _rounded_rect_mask(x, y, w, h, r):
    """True inside a rounded rectangle of size (w, h) with radius r."""
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), h - r)
    return hypot(x - cx, y - cy) <= r


def _ring(x, y, w, h, cx, cy, radius, thickness):
    """True inside an annulus."""
    d = hypot(x - cx, y - cy)
    return radius - thickness / 2 <= d <= radius + thickness / 2


def render(size):
    """Return size*size*4 bytes (BGRA) for a single icon size."""
    W = H = size * SS
    r = W * 0.22                                   # corner radius
    ring_r = W * 0.20
    # A ring thinner than ~1px disappears after downsampling at 16/24px,
    # so scale the thickness up for the smallest sizes.
    ring_t = max(W * 0.11, SS * 1.4)
    buf = bytearray(W * H * 4)
    for py in range(H):
        for px in range(W):
            i = (py * W + px) * 4
            if not _rounded_rect_mask(px + 0.5, py + 0.5, W, H, r):
                buf[i:i + 4] = b"\x00\x00\x00\x00"          # transparent
                continue
            # vertical gradient BG → BG_DARK
            t = py / (H - 1)
            cr = round(BG[0] * (1 - t) + BG_DARK[0] * t)
            cg = round(BG[1] * (1 - t) + BG_DARK[1] * t)
            cb = round(BG[2] * (1 - t) + BG_DARK[2] * t)
            if _ring(px + 0.5, py + 0.5, W, H, W / 2, H / 2, ring_r, ring_t):
                cr, cg, cb = INK
            buf[i:i + 4] = bytes((cb, cg, cr, 255))          # BGRA
    # box-downsample SSxSS -> size
    out = bytearray(size * size * 4)
    n = SS * SS
    for y in range(size):
        for x in range(size):
            a = r_ = g_ = b_ = 0
            for dy in range(SS):
                for dx in range(SS):
                    j = ((y * SS + dy) * W + (x * SS + dx)) * 4
                    b_ += buf[j + 0]; g_ += buf[j + 1]
                    r_ += buf[j + 2]; a += buf[j + 3]
            o = (y * size + x) * 4
            out[o + 0] = b_ // n
            out[o + 1] = g_ // n
            out[o + 2] = r_ // n
            out[o + 3] = a // n
    return out


def _dib(size, bgra):
    """A BMP-style DIB (BITMAPINFOHEADER + XOR mask + AND mask).

    For a 32bpp icon the DIB height is declared as **size * 2**: Windows
    expects `size` rows of XOR (colour) pixels followed by `size` rows of
    AND (transparency) mask. Declaring only `size` makes most decoders
    treat the file as truncated and show a blank square.
    """
    header = struct.pack(
        "<IiiHHIIiiII",
        40,                 # biSize
        size, size * 2,     # biWidth, biHeight = XOR rows + AND rows
        1, 32,              # biPlanes, biBitCount
        0, 0,               # biCompression, biSizeImage
        0, 0, 0, 0,         # dpi, colors used/important
    )
    # The DIB stores rows bottom-up, BGRA.
    rows = b"".join(
        bgra[(size - 1 - y) * size * 4:(size - y) * size * 4] for y in range(size)
    )
    # AND mask: 1 bit/pixel, padded to 32 bits per row. All 0 = opaque.
    stride = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (stride * size)
    return header + rows + and_mask


def main():
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "assets"
    out_dir.mkdir(exist_ok=True)
    ico = bytearray()
    blobs = []
    for s in SIZES:
        blobs.append((s, _dib(s, render(s))))
    # ICONDIR
    ico += struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    for s, data in blobs:
        w = 0 if s == 256 else s          # 0 means 256 in ICONDIRENTRY
        h = 0 if s == 256 else s
        ico += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in blobs:
        ico += data
    dest = out_dir / "omni.ico"
    dest.write_bytes(ico)
    print(f"wrote {dest} ({len(ico)} bytes, {len(blobs)} sizes: {', '.join(str(s) for s in SIZES)})")


if __name__ == "__main__":
    main()