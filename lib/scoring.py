"""
Visual fidelity scoring via multi-metric pixel comparison.

Compares a generated .icon bundle to the original .app icon by rendering
both via QuickLook, resizing to 256x256, and computing a weighted blend of
color RMSE, structural similarity (SSIM), and histogram correlation.
"""

import math
import os
import struct
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------------------
# Pixel I/O
# ---------------------------------------------------------------------------

def _read_bmp_pixels(path: str) -> list[tuple[int, int, int]]:
    """Read pixel data from a BMP file, compositing alpha onto white.

    Handles both 24-bit and 32-bit BMPs. For 32-bit, alpha is composited
    against a white background so comparisons are deterministic regardless
    of transparency handling.

    Returns list of (R, G, B) tuples in top-to-bottom, left-to-right order.
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:2] != b"BM":
        raise ValueError(f"Not a BMP file: {path}")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]

    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]

    if bpp not in (24, 32):
        raise ValueError(f"Unsupported BMP bpp={bpp} (expected 24 or 32)")

    bytes_per_pixel = bpp // 8
    abs_height = abs(height)
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
            if bpp == 32:
                a = data[offset + 3]
                # Composite onto white background: out = fg * alpha + bg * (1 - alpha)
                alpha = a / 255.0
                r = round(r * alpha + 255 * (1 - alpha))
                g = round(g * alpha + 255 * (1 - alpha))
                b = round(b * alpha + 255 * (1 - alpha))
            pixels.append((r, g, b))

    return pixels


# ---------------------------------------------------------------------------
# Metric 1: Color RMSE
# ---------------------------------------------------------------------------

def _color_rmse_score(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
) -> float:
    """Compute color RMSE between two pixel lists and return a 0-100 score.

    RMSE is computed across all three channels (R, G, B). A perfect match
    gives 100; an RMSE of >= 80 (out of 255) maps to 0.
    """
    n = len(pixels_a)
    if n == 0:
        return 0.0

    sum_sq = 0.0
    for (r1, g1, b1), (r2, g2, b2) in zip(pixels_a, pixels_b):
        sum_sq += (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2

    # Average over all pixels and all 3 channels
    mse = sum_sq / (n * 3)
    rmse = math.sqrt(mse)

    # Normalize: RMSE 0 -> score 100, RMSE >= max_rmse -> score 0
    max_rmse = 80.0
    score = max(0.0, 100.0 * (1.0 - rmse / max_rmse))
    return score


# ---------------------------------------------------------------------------
# Metric 2: Structural Similarity (SSIM)
# ---------------------------------------------------------------------------

def _to_luminance(pixels: list[tuple[int, int, int]]) -> list[float]:
    """Convert RGB pixels to luminance values (0-255 range)."""
    return [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]


def _ssim_score(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
    width: int,
    height: int,
) -> float:
    """Compute mean SSIM over 8x8 non-overlapping windows. Returns 0-100 score.

    Uses the standard SSIM formula with stabilization constants derived from
    a dynamic range of 255:
        C1 = (0.01 * 255)^2 = 6.5025
        C2 = (0.03 * 255)^2 = 58.5225
    """
    lum_a = _to_luminance(pixels_a)
    lum_b = _to_luminance(pixels_b)

    c1 = 6.5025   # (0.01 * 255)^2
    c2 = 58.5225  # (0.03 * 255)^2
    window = 8

    ssim_sum = 0.0
    count = 0

    for row in range(0, height - window + 1, window):
        for col in range(0, width - window + 1, window):
            # Gather pixel values in this window
            vals_a: list[float] = []
            vals_b: list[float] = []
            for wy in range(window):
                for wx in range(window):
                    idx = (row + wy) * width + (col + wx)
                    vals_a.append(lum_a[idx])
                    vals_b.append(lum_b[idx])

            n = len(vals_a)  # window * window = 64

            # Means
            mu_a = sum(vals_a) / n
            mu_b = sum(vals_b) / n

            # Variances and covariance
            var_a = sum((v - mu_a) ** 2 for v in vals_a) / n
            var_b = sum((v - mu_b) ** 2 for v in vals_b) / n
            cov_ab = sum((va - mu_a) * (vb - mu_b) for va, vb in zip(vals_a, vals_b)) / n

            # SSIM for this window
            numerator = (2 * mu_a * mu_b + c1) * (2 * cov_ab + c2)
            denominator = (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
            ssim_val = numerator / denominator

            ssim_sum += ssim_val
            count += 1

    if count == 0:
        return 0.0

    mean_ssim = ssim_sum / count
    # SSIM ranges from -1 to 1; in practice for similar images it's 0 to 1.
    # Map [0, 1] -> [0, 100]
    return max(0.0, min(100.0, mean_ssim * 100.0))


# ---------------------------------------------------------------------------
# Metric 3: Histogram Similarity
# ---------------------------------------------------------------------------

def _histogram_score(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
) -> float:
    """Compare RGB histograms using intersection similarity. Returns 0-100.

    Computes a 256-bin histogram for each channel, normalizes, and returns
    the average histogram intersection across R, G, B.
    """
    n = len(pixels_a)
    if n == 0:
        return 0.0

    total_score = 0.0

    for ch in range(3):  # R, G, B
        hist_a = [0] * 256
        hist_b = [0] * 256
        for pa, pb in zip(pixels_a, pixels_b):
            hist_a[pa[ch]] += 1
            hist_b[pb[ch]] += 1

        # Normalize to sum=1
        norm_a = [v / n for v in hist_a]
        norm_b = [v / n for v in hist_b]

        # Histogram intersection: sum of min(a, b) for each bin
        intersection = sum(min(a, b) for a, b in zip(norm_a, norm_b))
        total_score += intersection  # intersection is in [0, 1]

    # Average across 3 channels, scale to 0-100
    return (total_score / 3.0) * 100.0


# ---------------------------------------------------------------------------
# Combined scoring
# ---------------------------------------------------------------------------

_COMPARE_SIZE = 256

_WEIGHT_RMSE = 0.50
_WEIGHT_SSIM = 0.40
_WEIGHT_HISTOGRAM = 0.10


def score_visual_fidelity(icon_bundle: str, app_path: str) -> int:
    """Compare .icon bundle to .app icon visually using multi-metric scoring.

    Generates QuickLook thumbnails for both, resizes to 256x256 BMP,
    then computes a weighted blend of color RMSE, SSIM, and histogram
    similarity. Returns score 0-100.
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

        # Resize to 256x256 BMP via sips
        sz = str(_COMPARE_SIZE)
        for png, bmp in [(app_png, app_bmp), (icon_png, icon_bmp)]:
            result = subprocess.run(
                ["sips", "-z", sz, sz,
                 "-s", "format", "bmp", png, "--out", bmp],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not os.path.isfile(bmp):
                print(f"Error: sips resize failed: {result.stderr.strip()}", file=sys.stderr)
                return 0

        # Read pixel data
        try:
            app_pixels = _read_bmp_pixels(app_bmp)
            icon_pixels = _read_bmp_pixels(icon_bmp)
        except (ValueError, OSError) as e:
            print(f"Error: failed to read BMP: {e}", file=sys.stderr)
            return 0

        # Compute individual metrics
        rmse = _color_rmse_score(app_pixels, icon_pixels)
        ssim = _ssim_score(app_pixels, icon_pixels, _COMPARE_SIZE, _COMPARE_SIZE)
        hist = _histogram_score(app_pixels, icon_pixels)

        # Weighted blend
        score = _WEIGHT_RMSE * rmse + _WEIGHT_SSIM * ssim + _WEIGHT_HISTOGRAM * hist
        return max(0, min(100, round(score)))
