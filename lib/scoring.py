"""
Visual fidelity scoring via perceptual hashing (dHash).

Compares a generated .icon bundle to the original .app icon by rendering
both via QuickLook, resizing to 32x32, and computing a difference hash.
"""

import os
import subprocess
import sys
import tempfile


def _read_bmp_pixels(path: str) -> list[tuple[int, int, int]]:
    """Read pixel data from a BMP file. Returns list of (R,G,B) tuples, bottom-to-top row order."""
    import struct

    with open(path, "rb") as f:
        data = f.read()

    # BMP file header (14 bytes): signature, file size, reserved, pixel data offset
    if data[:2] != b"BM":
        raise ValueError(f"Not a BMP file: {path}")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]

    # DIB header: width, height, bits per pixel
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]

    if bpp not in (24, 32):
        raise ValueError(f"Unsupported BMP bpp={bpp} (expected 24 or 32)")

    bytes_per_pixel = bpp // 8
    abs_height = abs(height)
    # Each row is padded to a 4-byte boundary
    row_size = ((width * bytes_per_pixel + 3) // 4) * 4

    pixels: list[tuple[int, int, int]] = []
    for row in range(abs_height):
        # BMP stores rows bottom-to-top when height > 0
        if height > 0:
            y = abs_height - 1 - row
        else:
            y = row
        row_offset = pixel_offset + y * row_size
        for x in range(width):
            offset = row_offset + x * bytes_per_pixel
            b, g, r = data[offset], data[offset + 1], data[offset + 2]
            pixels.append((r, g, b))

    return pixels


def _dhash(pixels: list[tuple[int, int, int]], width: int, height: int) -> list[bool]:
    """Compute difference hash (dHash) from pixel data.

    Compares each pixel to the one to its right; produces (width-1)*height bits.
    """
    # Convert to grayscale
    gray = [int(0.299 * r + 0.587 * g + 0.114 * b) for r, g, b in pixels]

    bits: list[bool] = []
    for row in range(height):
        for col in range(width - 1):
            left = gray[row * width + col]
            right = gray[row * width + col + 1]
            bits.append(left > right)
    return bits


def _hamming_distance(a: list[bool], b: list[bool]) -> int:
    """Count differing bits between two hash lists."""
    return sum(x != y for x, y in zip(a, b))


def score_visual_fidelity(icon_bundle: str, app_path: str) -> int:
    """Compare .icon bundle to .app icon visually using perceptual hash (dHash).

    Generates QuickLook thumbnails for both, resizes to 32x32 BMP via sips,
    then computes dHash similarity. Returns score 0-100.
    """
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    thumbnail_bin = os.path.join(script_dir, "thumbnail")

    if not os.path.isfile(thumbnail_bin) or not os.access(thumbnail_bin, os.X_OK):
        print("Error: thumbnail binary not found (run recompose.sh first to compile it)", file=sys.stderr)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        app_png = os.path.join(tmp, "app.png")
        icon_png = os.path.join(tmp, "icon.png")
        app_bmp = os.path.join(tmp, "app.bmp")
        icon_bmp = os.path.join(tmp, "icon.bmp")

        # Generate QuickLook thumbnails
        for src, dst in [(app_path, app_png), (icon_bundle, icon_png)]:
            result = subprocess.run(
                [thumbnail_bin, src, dst],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not os.path.isfile(dst):
                print(f"Error: failed to generate thumbnail for {src}: {result.stderr.strip()}", file=sys.stderr)
                return 0

        # Resize to 32x32 BMP via sips
        hash_size = 32
        for png, bmp in [(app_png, app_bmp), (icon_png, icon_bmp)]:
            result = subprocess.run(
                ["sips", "-z", str(hash_size), str(hash_size),
                 "-s", "format", "bmp", png, "--out", bmp],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not os.path.isfile(bmp):
                print(f"Error: sips resize failed: {result.stderr.strip()}", file=sys.stderr)
                return 0

        # Read pixel data and compute dHash
        try:
            app_pixels = _read_bmp_pixels(app_bmp)
            icon_pixels = _read_bmp_pixels(icon_bmp)
        except (ValueError, OSError) as e:
            print(f"Error: failed to read BMP: {e}", file=sys.stderr)
            return 0

        app_hash = _dhash(app_pixels, hash_size, hash_size)
        icon_hash = _dhash(icon_pixels, hash_size, hash_size)

        distance = _hamming_distance(app_hash, icon_hash)
        total_bits = len(app_hash)  # (hash_size - 1) * hash_size = 31 * 32 = 992

        score = round(100 * (1 - distance / total_bits))
        return max(0, min(100, score))
