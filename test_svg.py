#!/usr/bin/env python3
"""
test_svg — visual fidelity testing suite for icon2svg output.

Renders an icon.svg via WebKit (thumbnail binary), then grades it
against both the .icon QuickLook preview and reference.png using the
same multi-metric scoring (RMSE, SSIM, histogram) from lib/scoring.py.

Usage:
    python3 test_svg.py Podcasts.icon                # single bundle
    python3 test_svg.py output/                      # batch (all *.icon)
    python3 test_svg.py Podcasts.icon --report       # also emit HTML report
    python3 test_svg.py Podcasts.icon --regenerate   # re-run icon2svg.py first
"""

import argparse
import base64
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

# Reuse pixel I/O and image preparation from the existing scoring module.
# Scoring functions are replaced by masked variants (below) that exclude
# the macOS icon superellipse boundary from comparison.
from lib.scoring import (
    _crop_and_resize,
    _read_bmp_pixels,
    _COMPARE_SIZE,
    _WEIGHT_RMSE,
    _WEIGHT_SSIM,
    _WEIGHT_HISTOGRAM,
)


# ---------------------------------------------------------------------------
# Tool compilation
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_THUMBNAIL_BIN = os.path.join(_SCRIPT_DIR, "thumbnail")
_THUMBNAIL_SRC = os.path.join(_SCRIPT_DIR, "thumbnail.swift")
_ICON2SVG = os.path.join(_SCRIPT_DIR, "icon2svg.py")


def _ensure_thumbnail() -> str:
    """Compile thumbnail.swift if the binary is missing or stale."""
    if (
        os.path.isfile(_THUMBNAIL_BIN)
        and os.access(_THUMBNAIL_BIN, os.X_OK)
        and os.path.getmtime(_THUMBNAIL_BIN) >= os.path.getmtime(_THUMBNAIL_SRC)
    ):
        return _THUMBNAIL_BIN

    print("Compiling thumbnail tool ...", file=sys.stderr)
    result = subprocess.run(
        [
            "swiftc", _THUMBNAIL_SRC,
            "-framework", "QuickLookThumbnailing",
            "-framework", "AppKit",
            "-framework", "WebKit",
            "-o", _THUMBNAIL_BIN,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error compiling thumbnail.swift:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return _THUMBNAIL_BIN


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _render_svg(thumbnail_bin: str, svg_path: str, out_png: str) -> bool:
    """Render an SVG to PNG via the thumbnail binary (WebKit path)."""
    result = subprocess.run(
        [thumbnail_bin, svg_path, out_png],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error rendering SVG: {result.stderr.strip()}", file=sys.stderr)
        return False
    return os.path.isfile(out_png)


def _render_quicklook(thumbnail_bin: str, bundle_dir: str, out_png: str) -> bool:
    """Render an .icon bundle to PNG via the thumbnail binary (QuickLook path)."""
    result = subprocess.run(
        [thumbnail_bin, bundle_dir, out_png],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error generating QuickLook: {result.stderr.strip()}", file=sys.stderr)
        return False
    return os.path.isfile(out_png)


# ---------------------------------------------------------------------------
# Alpha-masked scoring — excludes macOS icon superellipse boundaries
# ---------------------------------------------------------------------------

def _read_bmp_alpha_mask(path: str, threshold: int = 250) -> list[bool]:
    """Read a 32-bit BMP and return a boolean mask from its alpha channel.

    Returns a list of bools (True = pixel inside icon shape, False =
    transparent corner).  For 24-bit BMPs (no alpha), returns all-True.
    Pixel order matches _read_bmp_pixels (top-to-bottom, left-to-right).
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:2] != b"BM":
        raise ValueError(f"Not a BMP file: {path}")

    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]

    if bpp == 24:
        # No alpha channel — all pixels are valid
        return [True] * (width * abs(height))

    if bpp != 32:
        raise ValueError(f"Unsupported BMP bpp={bpp}")

    bytes_per_pixel = 4
    abs_height = abs(height)
    row_size = ((width * bytes_per_pixel + 3) // 4) * 4

    mask: list[bool] = []
    for row in range(abs_height):
        y = abs_height - 1 - row if height > 0 else row
        row_offset = pixel_offset + y * row_size
        for x in range(width):
            offset = row_offset + x * bytes_per_pixel
            a = data[offset + 3]
            mask.append(a >= threshold)

    return mask


def _masked_rmse(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
    mask: list[bool],
) -> float:
    """Compute color RMSE only over masked pixels. Returns 0-100 score."""
    sum_sq = 0.0
    n = 0
    for i, m in enumerate(mask):
        if not m:
            continue
        r1, g1, b1 = pixels_a[i]
        r2, g2, b2 = pixels_b[i]
        sum_sq += (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2
        n += 1

    if n == 0:
        return 0.0
    mse = sum_sq / (n * 3)
    rmse = math.sqrt(mse)
    max_rmse = 80.0
    return max(0.0, 100.0 * (1.0 - rmse / max_rmse))


def _masked_ssim(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
    width: int,
    height: int,
    mask: list[bool],
) -> float:
    """Compute mean SSIM over 8x8 windows, skipping windows that overlap
    masked-out pixels (icon boundary). Returns 0-100 score."""
    lum_a = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels_a]
    lum_b = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels_b]

    c1 = 6.5025   # (0.01 * 255)^2
    c2 = 58.5225  # (0.03 * 255)^2
    window = 8

    ssim_sum = 0.0
    count = 0

    for row in range(0, height - window + 1, window):
        for col in range(0, width - window + 1, window):
            # Check if all pixels in this window are inside the mask
            all_valid = True
            vals_a: list[float] = []
            vals_b: list[float] = []
            for wy in range(window):
                for wx in range(window):
                    idx = (row + wy) * width + (col + wx)
                    if not mask[idx]:
                        all_valid = False
                        break
                    vals_a.append(lum_a[idx])
                    vals_b.append(lum_b[idx])
                if not all_valid:
                    break

            if not all_valid:
                continue

            n = len(vals_a)
            mu_a = sum(vals_a) / n
            mu_b = sum(vals_b) / n
            var_a = sum((v - mu_a) ** 2 for v in vals_a) / n
            var_b = sum((v - mu_b) ** 2 for v in vals_b) / n
            cov_ab = sum(
                (va - mu_a) * (vb - mu_b) for va, vb in zip(vals_a, vals_b)
            ) / n

            numerator = (2 * mu_a * mu_b + c1) * (2 * cov_ab + c2)
            denominator = (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
            ssim_sum += numerator / denominator
            count += 1

    if count == 0:
        return 0.0
    mean_ssim = ssim_sum / count
    return max(0.0, min(100.0, mean_ssim * 100.0))


def _masked_histogram(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
    mask: list[bool],
) -> float:
    """Compare RGB histograms using only masked pixels. Returns 0-100."""
    n = 0
    total_score = 0.0

    for ch in range(3):
        hist_a = [0] * 256
        hist_b = [0] * 256
        count = 0
        for i, m in enumerate(mask):
            if not m:
                continue
            hist_a[pixels_a[i][ch]] += 1
            hist_b[pixels_b[i][ch]] += 1
            count += 1

        if count == 0:
            continue

        norm_a = [v / count for v in hist_a]
        norm_b = [v / count for v in hist_b]
        intersection = sum(min(a, b) for a, b in zip(norm_a, norm_b))
        total_score += intersection
        n += 1

    if n == 0:
        return 0.0
    return (total_score / n) * 100.0


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _bmp_to_png_base64(bmp_path: str, tmp_dir: str) -> str | None:
    """Convert a BMP to PNG via sips and return a data URI, or None."""
    png_path = bmp_path.replace(".bmp", "_out.png")
    result = subprocess.run(
        ["sips", "-s", "format", "png", bmp_path, "--out", png_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.isfile(png_path):
        return None
    with open(png_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _compare_images(
    png_a: str,
    png_b: str,
    tmp_dir: str,
    label_a: str = "a",
    label_b: str = "b",
    collect_pngs: bool = False,
) -> dict | None:
    """Crop, resize, and score two PNG images.

    Returns dict with keys: rmse, ssim, hist, score  (all 0-100).
    When collect_pngs is True, also includes 'png_a' and 'png_b' keys
    with base64 data URIs of the cropped/resized images used for scoring.
    Returns None on failure.
    """
    bmp_a = os.path.join(tmp_dir, f"{label_a}.bmp")
    bmp_b = os.path.join(tmp_dir, f"{label_b}.bmp")

    if not _crop_and_resize(png_a, bmp_a, _COMPARE_SIZE, tmp_dir):
        print(f"Error: crop/resize failed for {png_a}", file=sys.stderr)
        return None
    if not _crop_and_resize(png_b, bmp_b, _COMPARE_SIZE, tmp_dir):
        print(f"Error: crop/resize failed for {png_b}", file=sys.stderr)
        return None

    try:
        pix_a = _read_bmp_pixels(bmp_a)
        pix_b = _read_bmp_pixels(bmp_b)
        # Use image B's alpha channel (QuickLook/Reference) to mask out
        # the macOS icon superellipse corners so they don't dominate scoring.
        mask = _read_bmp_alpha_mask(bmp_b, threshold=250)
    except (ValueError, OSError) as e:
        print(f"Error reading BMP: {e}", file=sys.stderr)
        return None

    rmse = _masked_rmse(pix_a, pix_b, mask)
    ssim = _masked_ssim(pix_a, pix_b, _COMPARE_SIZE, _COMPARE_SIZE, mask)
    hist = _masked_histogram(pix_a, pix_b, mask)
    score = _WEIGHT_RMSE * rmse + _WEIGHT_SSIM * ssim + _WEIGHT_HISTOGRAM * hist
    score = max(0, min(100, round(score)))

    result = {"rmse": round(rmse), "ssim": round(ssim), "hist": round(hist), "score": score}

    if collect_pngs:
        result["png_a"] = _bmp_to_png_base64(bmp_a, tmp_dir)
        result["png_b"] = _bmp_to_png_base64(bmp_b, tmp_dir)

    return result


# ---------------------------------------------------------------------------
# Per-bundle test
# ---------------------------------------------------------------------------

def test_bundle(
    bundle_dir: str,
    thumbnail_bin: str,
    regenerate: bool = False,
    report: bool = False,
) -> dict | None:
    """Run the full test flow for a single .icon bundle.

    Returns dict with keys:
        name, svg_vs_ref, svg_vs_ql, svg_png, ql_png, ref_png
    or None on failure.
    """
    bundle_dir = bundle_dir.rstrip("/")
    name = os.path.basename(bundle_dir)

    icon_json = os.path.join(bundle_dir, "icon.json")
    if not os.path.isfile(icon_json):
        print(f"Skip {name}: no icon.json", file=sys.stderr)
        return None

    svg_path = os.path.join(bundle_dir, "icon.svg")

    # Regenerate SVG if requested or missing
    if regenerate or not os.path.isfile(svg_path):
        result = subprocess.run(
            [sys.executable, _ICON2SVG, bundle_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Error generating SVG for {name}: {result.stderr.strip()}", file=sys.stderr)
            return None

    if not os.path.isfile(svg_path):
        print(f"Skip {name}: no icon.svg", file=sys.stderr)
        return None

    ref_png = os.path.join(bundle_dir, "reference.png")
    has_ref = os.path.isfile(ref_png)

    with tempfile.TemporaryDirectory() as tmp:
        svg_png = os.path.join(tmp, "svg.png")
        ql_png = os.path.join(tmp, "ql.png")

        # Render SVG to PNG (WebKit)
        if not _render_svg(thumbnail_bin, svg_path, svg_png):
            print(f"Error: SVG render failed for {name}", file=sys.stderr)
            return None

        # Render .icon to PNG (QuickLook)
        if not _render_quicklook(thumbnail_bin, bundle_dir, ql_png):
            print(f"Error: QuickLook render failed for {name}", file=sys.stderr)
            return None

        # Score: SVG vs Reference
        svg_vs_ref = None
        if has_ref:
            svg_vs_ref = _compare_images(
                svg_png, ref_png, tmp, "svg_r", "ref_r", collect_pngs=report
            )

        # Score: SVG vs QuickLook
        svg_vs_ql = _compare_images(
            svg_png, ql_png, tmp, "svg_q", "ql_q", collect_pngs=report
        )

        # Build report images from the cropped/resized versions so all
        # three are at the same 256x256 scale used for scoring.
        result_pngs = {}
        if report:
            # SVG cropped — use the one from whichever comparison ran
            src = svg_vs_ref or svg_vs_ql
            if src and src.get("png_a"):
                result_pngs["svg"] = src["png_a"]
            # QuickLook cropped
            if svg_vs_ql and svg_vs_ql.get("png_b"):
                result_pngs["ql"] = svg_vs_ql["png_b"]
            # Reference cropped
            if svg_vs_ref and svg_vs_ref.get("png_b"):
                result_pngs["ref"] = svg_vs_ref["png_b"]

    return {
        "name": name,
        "svg_vs_ref": svg_vs_ref,
        "svg_vs_ql": svg_vs_ql,
        "pngs": result_pngs if report else {},
    }


def _png_to_base64(path: str) -> str:
    """Read a PNG file and return a data URI."""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_score_line(label: str, scores: dict | None) -> str:
    """Format one comparison pair as a single line."""
    if scores is None:
        return f"  {label}:  (unavailable)"
    return (
        f"  {label}:  "
        f"RMSE {scores['rmse']:>3}  "
        f"SSIM {scores['ssim']:>3}  "
        f"Hist {scores['hist']:>3}  "
        f"-> {scores['score']:>3}"
    )


def print_single(result: dict) -> None:
    """Print detailed results for a single bundle."""
    print(result["name"])
    print(_format_score_line("SVG vs Reference", result["svg_vs_ref"]))
    print(_format_score_line("SVG vs QuickLook", result["svg_vs_ql"]))


def print_batch(results: list[dict]) -> None:
    """Print a summary table for multiple bundles."""
    # Header
    print(f"{'Bundle':<30s} {'SVG vs Ref':>10s} {'SVG vs QL':>10s}")
    print("-" * 52)
    for r in results:
        ref = str(r["svg_vs_ref"]["score"]) if r["svg_vs_ref"] else "-"
        ql = str(r["svg_vs_ql"]["score"]) if r["svg_vs_ql"] else "-"
        print(f"{r['name']:<30s} {ref:>10s} {ql:>10s}")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SVG Test Report</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2em; background: #f5f5f7; color: #1d1d1f; }}
  h1 {{ font-size: 1.5em; }}
  .bundle {{ background: #fff; border-radius: 12px; padding: 1.5em; margin-bottom: 2em; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .bundle h2 {{ margin-top: 0; font-size: 1.2em; }}
  .images {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1em; margin: 1em 0; }}
  .images figure {{ margin: 0; text-align: center; }}
  .images img {{ width: 100%; aspect-ratio: 1; object-fit: contain; border-radius: 8px; border: 1px solid #e0e0e0; background: #f0f0f0; }}
  .images figcaption {{ font-size: 0.85em; color: #666; margin-top: 0.5em; }}
  table {{ border-collapse: collapse; font-size: 0.9em; }}
  th, td {{ padding: 0.4em 1em; text-align: right; }}
  th {{ text-align: left; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
  .score {{ font-weight: 600; }}
</style>
</head>
<body>
<h1>SVG Test Report</h1>
{content}
</body>
</html>
"""

_BUNDLE_TEMPLATE = """\
<div class="bundle">
  <h2>{name}</h2>
  <div class="images">
    {images}
  </div>
  <table>
    <tr><th>Comparison</th><th>RMSE</th><th>SSIM</th><th>Hist</th><th class="score">Score</th></tr>
    {rows}
  </table>
</div>
"""


def _score_row(label: str, scores: dict | None) -> str:
    if scores is None:
        return f"<tr><th>{label}</th><td colspan='4'>unavailable</td></tr>"
    return (
        f"<tr><th>{label}</th>"
        f"<td>{scores['rmse']}</td>"
        f"<td>{scores['ssim']}</td>"
        f"<td>{scores['hist']}</td>"
        f"<td class='score'>{scores['score']}</td></tr>"
    )


def generate_html_report(results: list[dict], out_path: str) -> None:
    """Write a self-contained HTML report with embedded images."""
    bundles_html: list[str] = []

    for r in results:
        pngs = r.get("pngs", {})
        figs: list[str] = []
        if "svg" in pngs:
            figs.append(
                f'<figure><img src="{pngs["svg"]}"><figcaption>SVG Render</figcaption></figure>'
            )
        if "ql" in pngs:
            figs.append(
                f'<figure><img src="{pngs["ql"]}"><figcaption>QuickLook</figcaption></figure>'
            )
        if "ref" in pngs:
            figs.append(
                f'<figure><img src="{pngs["ref"]}"><figcaption>Reference</figcaption></figure>'
            )

        rows = "\n    ".join([
            _score_row("SVG vs Reference", r["svg_vs_ref"]),
            _score_row("SVG vs QuickLook", r["svg_vs_ql"]),
        ])

        bundles_html.append(
            _BUNDLE_TEMPLATE.format(
                name=r["name"],
                images="\n    ".join(figs),
                rows=rows,
            )
        )

    html = _HTML_TEMPLATE.format(content="\n".join(bundles_html))
    with open(out_path, "w") as f:
        f.write(html)
    print(f"Report written to {out_path}")


# ---------------------------------------------------------------------------
# Bundle discovery
# ---------------------------------------------------------------------------

def discover_bundles(path: str) -> list[str]:
    """Return a list of .icon bundle directories under path."""
    if path.endswith(".icon") and os.path.isdir(path):
        return [path]
    if os.path.isdir(path):
        bundles = sorted(
            os.path.join(path, d)
            for d in os.listdir(path)
            if d.endswith(".icon") and os.path.isdir(os.path.join(path, d))
        )
        return bundles
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test SVG output against reference.png and QuickLook preview"
    )
    parser.add_argument(
        "path",
        help="Path to a .icon bundle or a directory containing .icon bundles",
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Re-run icon2svg.py before testing",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate an HTML report with side-by-side image comparison",
    )
    args = parser.parse_args()

    thumbnail_bin = _ensure_thumbnail()
    bundles = discover_bundles(args.path)

    if not bundles:
        print(f"No .icon bundles found at {args.path}", file=sys.stderr)
        sys.exit(1)

    results: list[dict] = []
    for bundle in bundles:
        r = test_bundle(bundle, thumbnail_bin, args.regenerate, args.report)
        if r:
            results.append(r)

    if not results:
        print("No bundles could be tested.", file=sys.stderr)
        sys.exit(1)

    # Print results
    if len(results) == 1:
        print_single(results[0])
    else:
        print_batch(results)
        print()
        for r in results:
            print_single(r)
            print()

    # HTML report
    if args.report:
        if len(bundles) == 1:
            report_path = os.path.join(bundles[0], "test_report.html")
        else:
            report_path = os.path.join(args.path, "test_report.html")
        generate_html_report(results, report_path)


if __name__ == "__main__":
    main()
