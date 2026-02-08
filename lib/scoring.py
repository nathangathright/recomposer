"""
Visual fidelity scoring via multi-metric pixel comparison.

Compares a generated .icon bundle to the original .app icon by:
1. Using the pre-rendered reference.png from Assets.car (preferred) or
   falling back to a QuickLook thumbnail of the .app bundle.
2. Generating a QuickLook thumbnail of the .icon bundle.
3. Cropping both images to their content bounds (trimming transparent
   padding) so framing differences don't penalize the score.
4. Resizing both to 256x256 and computing a weighted blend of color RMSE,
   structural similarity (SSIM), and histogram correlation.
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
# Content-bounds cropping
# ---------------------------------------------------------------------------

def _read_bmp_raw(path: str) -> tuple[bytes, int, int, int, int, int]:
    """Read raw BMP metadata. Returns (data, pixel_offset, width, height, bpp, row_size)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"BM":
        raise ValueError(f"Not a BMP file: {path}")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    bytes_per_pixel = bpp // 8
    row_size = ((width * bytes_per_pixel + 3) // 4) * 4
    return data, pixel_offset, width, height, bpp, row_size


def _find_content_bounds(bmp_path: str) -> tuple[int, int, int, int] | None:
    """Find the bounding box of non-transparent pixels in a 32-bit BMP.

    Returns (x, y, width, height) of the content region, or None if the
    image has no alpha channel or no transparent pixels.
    """
    data, pixel_offset, width, height, bpp, row_size = _read_bmp_raw(bmp_path)
    if bpp != 32:
        return None

    abs_height = abs(height)
    min_x, max_x = width, 0
    min_y, max_y = abs_height, 0
    bytes_per_pixel = 4

    for row in range(abs_height):
        y = abs_height - 1 - row if height > 0 else row
        row_offset = pixel_offset + y * row_size
        for x in range(width):
            offset = row_offset + x * bytes_per_pixel
            a = data[offset + 3]
            if a > 10:  # non-transparent pixel
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, row)
                max_y = max(max_y, row)

    if max_x < min_x:
        return None  # entirely transparent

    return min_x, min_y, max_x - min_x + 1, max_y - min_y + 1


def _crop_and_resize(png_path: str, bmp_out: str, size: int, tmp_dir: str) -> bool:
    """Crop a PNG to its content bounds and resize to size x size BMP.

    Steps:
    1. Convert to 32-bit BMP to find content bounds via alpha channel.
    2. Use sips to crop to content bounds (separate call).
    3. Resize the cropped result to the target size.

    Returns True on success.
    """
    # Convert to BMP to detect content bounds
    probe_bmp = os.path.join(tmp_dir, "probe.bmp")
    result = subprocess.run(
        ["sips", "-s", "format", "bmp", png_path, "--out", probe_bmp],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.isfile(probe_bmp):
        return False

    bounds = _find_content_bounds(probe_bmp)
    os.remove(probe_bmp)

    sz = str(size)

    if bounds:
        cx, cy, cw, ch = bounds
        # Step 1: crop to content bounds (produces a cw x ch intermediate)
        cropped = os.path.join(tmp_dir, "cropped.png")
        result = subprocess.run(
            ["sips",
             "--cropOffset", str(cy), str(cx),
             "-c", str(ch), str(cw),
             png_path, "--out", cropped],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not os.path.isfile(cropped):
            return False
        # Step 2: resize to target size and convert to BMP
        result = subprocess.run(
            ["sips", "-z", sz, sz,
             "-s", "format", "bmp", cropped, "--out", bmp_out],
            capture_output=True, text=True,
        )
        os.remove(cropped)
    else:
        # No alpha or no transparent pixels — just resize directly
        result = subprocess.run(
            ["sips", "-z", sz, sz,
             "-s", "format", "bmp", png_path, "--out", bmp_out],
            capture_output=True, text=True,
        )

    return result.returncode == 0 and os.path.isfile(bmp_out)


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

    Uses the pre-rendered reference.png from the bundle (extracted from
    Assets.car) as the ground-truth image when available, falling back to
    a QuickLook thumbnail of the .app bundle.  The .icon side always uses
    QuickLook (which renders the Icon Composer document).

    Both images are cropped to their content bounds (trimming transparent
    padding) before resizing to 256x256 for comparison.  This eliminates
    score penalties from framing/padding differences between the reference
    and the Icon Composer renderer.

    Returns score 0-100.
    """
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    thumbnail_bin = os.path.join(script_dir, "thumbnail")

    if not os.path.isfile(thumbnail_bin) or not os.access(thumbnail_bin, os.X_OK):
        print("Error: thumbnail binary not found (run recompose.sh first to compile it)", file=sys.stderr)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        ref_png = os.path.join(tmp, "ref.png")
        icon_png = os.path.join(tmp, "icon.png")
        ref_bmp = os.path.join(tmp, "ref.bmp")
        icon_bmp = os.path.join(tmp, "icon.bmp")

        # --- Reference image (ground truth) ---
        # Prefer the pre-rendered Icon Image extracted from Assets.car
        bundle_ref = os.path.join(icon_bundle, "reference.png")
        if os.path.isfile(bundle_ref):
            # Copy to tmp so sips can work on it without modifying the bundle
            import shutil
            shutil.copy2(bundle_ref, ref_png)
        else:
            # Fallback: QuickLook thumbnail of the .app bundle
            result = subprocess.run(
                [thumbnail_bin, app_path, ref_png],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not os.path.isfile(ref_png):
                print(f"Error: failed to generate thumbnail for {app_path}: {result.stderr.strip()}", file=sys.stderr)
                return 0

        # --- Icon Composer rendering ---
        result = subprocess.run(
            [thumbnail_bin, icon_bundle, icon_png],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not os.path.isfile(icon_png):
            print(f"Error: failed to generate thumbnail for {icon_bundle}: {result.stderr.strip()}", file=sys.stderr)
            return 0

        # Crop to content bounds and resize to comparison size
        if not _crop_and_resize(ref_png, ref_bmp, _COMPARE_SIZE, tmp):
            print("Error: failed to crop/resize reference image", file=sys.stderr)
            return 0
        if not _crop_and_resize(icon_png, icon_bmp, _COMPARE_SIZE, tmp):
            print("Error: failed to crop/resize icon image", file=sys.stderr)
            return 0

        # Read pixel data
        try:
            ref_pixels = _read_bmp_pixels(ref_bmp)
            icon_pixels = _read_bmp_pixels(icon_bmp)
        except (ValueError, OSError) as e:
            print(f"Error: failed to read BMP: {e}", file=sys.stderr)
            return 0

        # Compute individual metrics
        rmse = _color_rmse_score(ref_pixels, icon_pixels)
        ssim = _ssim_score(ref_pixels, icon_pixels, _COMPARE_SIZE, _COMPARE_SIZE)
        hist = _histogram_score(ref_pixels, icon_pixels)

        # Weighted blend
        score = _WEIGHT_RMSE * rmse + _WEIGHT_SSIM * ssim + _WEIGHT_HISTOGRAM * hist
        return max(0, min(100, round(score)))
