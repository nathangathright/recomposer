#!/usr/bin/env python3
"""
icon2svg — convert a .icon bundle to SVG.

Reads icon.json and Assets/ from a .icon bundle directory and writes
icon.svg alongside them.  SVG assets are inlined directly (inner
content extracted and embedded) so that SVG filters like
feSpecularLighting can access the pixel data.  PNG assets are
base64-encoded as data: URIs.  CSS custom properties with a
@media (color-gamut: p3) block provide wide-gamut color support.

Usage:
    python3 icon2svg.py Podcasts.icon        # -> Podcasts.icon/icon.svg
"""

import base64
import json
import os
import sys


# ---------------------------------------------------------------------------
# P3-to-sRGB color conversion
# ---------------------------------------------------------------------------

def _display_p3_to_srgb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert Display P3 color components to sRGB.

    Inverse of catalog.py's _srgb_to_display_p3.  Converts through
    CIE XYZ (D65) and clamps to 0-1 for sRGB output.
    """
    def linearize(v: float) -> float:
        sign = 1.0 if v >= 0 else -1.0
        v = abs(v)
        if v <= 0.04045:
            return sign * v / 12.92
        return sign * ((v + 0.055) / 1.055) ** 2.4

    def gamma_encode(v: float) -> float:
        sign = 1.0 if v >= 0 else -1.0
        v = abs(v)
        if v <= 0.0031308:
            return sign * v * 12.92
        return sign * (1.055 * v ** (1.0 / 2.4) - 0.055)

    # 1. Linearize P3 (same transfer function as sRGB)
    rl, gl, bl = linearize(r), linearize(g), linearize(b)
    # 2. Display P3 linear -> XYZ (D65)
    x = 0.4865709 * rl + 0.2656677 * gl + 0.1982173 * bl
    y = 0.2289746 * rl + 0.6917385 * gl + 0.0792869 * bl
    z = 0.0000000 * rl + 0.0451134 * gl + 1.0439444 * bl
    # 3. XYZ (D65) -> sRGB linear
    sr = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    sg = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    sb = 0.0556434 * x - 0.2040260 * y + 1.0572252 * z
    # 4. Apply sRGB gamma and clamp
    return (
        max(0.0, min(1.0, gamma_encode(sr))),
        max(0.0, min(1.0, gamma_encode(sg))),
        max(0.0, min(1.0, gamma_encode(sb))),
    )


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------

def _parse_color_string(color_str: str) -> tuple[float, float, float, float, bool]:
    """Parse 'display-p3:r,g,b,a' or 'extended-gray:g,a'.

    Returns (r, g, b, a, is_gray).
    For gray colors, r == g == b == gray_value.
    """
    if color_str.startswith("extended-gray:"):
        parts = color_str[len("extended-gray:"):].split(",")
        gray = float(parts[0])
        alpha = float(parts[1]) if len(parts) > 1 else 1.0
        return gray, gray, gray, alpha, True
    if color_str.startswith("display-p3:"):
        parts = color_str[len("display-p3:"):].split(",")
        r = float(parts[0])
        g = float(parts[1]) if len(parts) > 1 else 0.0
        b = float(parts[2]) if len(parts) > 2 else 0.0
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return r, g, b, a, False
    # Unknown format — mid-gray fallback
    return 0.5, 0.5, 0.5, 1.0, False


# ---------------------------------------------------------------------------
# Color registry — assigns CSS custom properties to unique colors
# ---------------------------------------------------------------------------

class ColorRegistry:
    """Collect unique color strings and generate CSS custom properties."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}   # color_str -> "--cN"
        self._order: list[str] = []      # insertion order
        self._counter: int = 0

    def register(self, color_str: str) -> str:
        """Register a color and return its CSS var() reference."""
        if color_str not in self._map:
            var_name = f"--c{self._counter}"
            self._map[color_str] = var_name
            self._order.append(color_str)
            self._counter += 1
        return f"var({self._map[color_str]})"

    def css_block(self) -> str:
        """Return the <style>…</style> block with sRGB defaults and P3 override."""
        if not self._order:
            return ""

        srgb_lines: list[str] = []
        p3_lines: list[str] = []

        for color_str in self._order:
            var_name = self._map[color_str]
            r, g, b, a, is_gray = _parse_color_string(color_str)

            # --- sRGB fallback ---
            if is_gray:
                sr, sg, sb = r, g, b
            else:
                sr, sg, sb = _display_p3_to_srgb(r, g, b)
            ri = max(0, min(255, round(sr * 255)))
            gi = max(0, min(255, round(sg * 255)))
            bi = max(0, min(255, round(sb * 255)))
            if abs(a - 1.0) < 0.001:
                srgb_css = f"rgb({ri}, {gi}, {bi})"
            else:
                srgb_css = f"rgba({ri}, {gi}, {bi}, {a:.4f})"

            # --- P3 value ---
            if abs(a - 1.0) < 0.001:
                p3_css = f"color(display-p3 {r:.5f} {g:.5f} {b:.5f})"
            else:
                p3_css = f"color(display-p3 {r:.5f} {g:.5f} {b:.5f} / {a:.5f})"

            srgb_lines.append(f"      {var_name}: {srgb_css};")
            p3_lines.append(f"        {var_name}: {p3_css};")

        lines = [
            "  <style>",
            "    :root {",
            *srgb_lines,
            "    }",
            "    @media (color-gamut: p3) {",
            "      :root {",
            *p3_lines,
            "      }",
            "    }",
            "  </style>",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SVG gradient / fill helpers
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    """Escape a string for use in XML attribute values."""
    return (s.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _read_asset_inline(bundle_dir: str, image_name: str) -> tuple[str | None, str]:
    """Read an asset for inlining into the SVG document.

    For SVG assets: extracts content between <svg> and </svg> tags.
    Returns (inner_content, "svg").

    For PNG assets: base64-encodes the file data.
    Returns (data_uri, "png").

    On failure: returns (None, "").
    """
    path = os.path.join(bundle_dir, "Assets", image_name)
    if not os.path.isfile(path):
        return None, ""

    ext = os.path.splitext(image_name)[1].lower()

    if ext == ".svg":
        with open(path) as f:
            content = f.read()
        start = content.find("<svg")
        if start < 0:
            return None, ""
        end_open = content.find(">", start)
        if end_open < 0:
            return None, ""
        close = content.rfind("</svg>")
        if close < 0:
            return None, ""
        return content[end_open + 1 : close].strip(), "svg"

    if ext == ".png":
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{data}", "png"

    return None, ""


def _inline_asset_element(
    bundle_dir: str,
    image_name: str,
    indent: str,
    op_attr: str = "",
) -> str:
    """Return inline SVG markup for an asset.

    For SVG assets, embeds the inner content directly in a <g> wrapper.
    For PNG assets, uses a data: URI in an <image>.
    Falls back to a relative href on failure.
    """
    content, atype = _read_asset_inline(bundle_dir, image_name)

    if atype == "svg" and content is not None:
        return f"{indent}<g{op_attr}>\n{content}\n{indent}</g>"

    # PNG data URI or fallback to relative href
    if atype == "png" and content is not None:
        href = content
    else:
        href = _xml_escape(f"Assets/{image_name}")

    return f'{indent}<image href="{href}" width="1024" height="1024"{op_attr}/>'


def _gradient_element(
    grad_id: str,
    color_vars: list[str],
    orientation: dict | None,
) -> str:
    """Build a <linearGradient> element."""
    x1, y1 = 0.5, 0.0
    x2, y2 = 0.5, 1.0
    if orientation:
        start = orientation.get("start", {})
        stop = orientation.get("stop", {})
        x1 = start.get("x", 0.5)
        y1 = start.get("y", 0.0)
        x2 = stop.get("x", 0.5)
        y2 = stop.get("y", 1.0)

    parts = [
        f'      <linearGradient id="{grad_id}" '
        f'gradientUnits="objectBoundingBox" '
        f'x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
    ]
    n = len(color_vars)
    for i, cv in enumerate(color_vars):
        offset = i / max(n - 1, 1)
        parts.append(f'        <stop offset="{offset}" stop-color="{cv}"/>')
    parts.append("      </linearGradient>")
    return "\n".join(parts)


def _register_fill(
    fill: dict,
    grad_id: str,
    colors: ColorRegistry,
) -> tuple[str, str]:
    """Register colors in a fill dict and build the gradient element.

    Returns (gradient_element_str, fill_attr_value).
    """
    if "linear-gradient" in fill:
        stops = fill["linear-gradient"]
        orientation = fill.get("orientation")
        var_refs = [colors.register(s) for s in stops]
        elem = _gradient_element(grad_id, var_refs, orientation)
        return elem, f"url(#{grad_id})"

    if "automatic-gradient" in fill:
        color_str = fill["automatic-gradient"]
        var_ref = colors.register(color_str)
        elem = _gradient_element(grad_id, [var_ref, var_ref], None)
        return elem, f"url(#{grad_id})"

    return "", "gray"


# ---------------------------------------------------------------------------
# SVG filter helpers
# ---------------------------------------------------------------------------

def _shadow_filter(group_idx: int, kind: str, opacity: float) -> str:
    """Build a shadow <filter> element.  Returns '' for kind='none'.

    Shadow parameters are tuned for a 1024×1024 canvas.  The icon.json
    shadow opacity is scaled down (×0.35) because the raw catalog value
    represents "full designed strength", not a literal alpha — using it
    raw produces an unrealistically dark shadow.
    """
    if kind == "none":
        return ""

    fid = f"shadow-g{group_idx}"
    slope = round(opacity * 0.35, 4)
    # Use SourceAlpha for neutral (black shadow), SourceGraphic for chromatic
    blur_input = "SourceAlpha" if kind == "neutral" else "SourceGraphic"

    return "\n".join([
        f'      <filter id="{fid}" x="-30%" y="-30%" width="160%" height="160%">',
        f'        <feGaussianBlur in="{blur_input}" stdDeviation="16" result="blur"/>',
        f'        <feOffset in="blur" dy="10" result="offset"/>',
        f'        <feComponentTransfer in="offset" result="shadow">',
        f'          <feFuncA type="linear" slope="{slope}"/>',
        f'        </feComponentTransfer>',
        f'        <feMerge>',
        f'          <feMergeNode in="shadow"/>',
        f'          <feMergeNode in="SourceGraphic"/>',
        f'        </feMerge>',
        f'      </filter>',
    ])


def _specular_filter(group_idx: int) -> str:
    """Build the glass <filter> — rim lighting is the primary effect.

    The filter creates a prominent bright inner-edge glow (~6 px wide)
    by eroding SourceAlpha inward, blurring the boundary, and
    subtracting from the original alpha to isolate a soft rim band.
    White is flooded through this rim mask to produce a bright edge.

    Content is dimmed ~10 % via feColorMatrix so the white rim has
    contrast headroom against the (white) layer shapes.

    A subtle feSpecularLighting adds directional top-light shine as a
    secondary effect.  Both rim and specular are composited over the
    dimmed content.
    """
    fid = f"specular-g{group_idx}"
    return "\n".join([
        f'      <filter id="{fid}">',

        # --- 1. Mild dim for contrast against bright rim ---
        f'        <feColorMatrix in="SourceGraphic" type="matrix"',
        f'          values="0.9 0 0 0 0  0 0.9 0 0 0  0 0 0.9 0 0  0 0 0 1 0"',
        f'          result="dimmed"/>',

        # --- 2. Rim lighting (primary effect) ---
        # Erode alpha 4 px inward, blur the inner boundary (2 px),
        # then subtract from original alpha → soft ~6 px rim band.
        f'        <feMorphology in="SourceAlpha" operator="erode" radius="4" result="shrunk"/>',
        f'        <feGaussianBlur in="shrunk" stdDeviation="2" result="shrunkBlur"/>',
        f'        <feComposite in="SourceAlpha" in2="shrunkBlur" operator="arithmetic"',
        f'                     k1="0" k2="1" k3="-1" k4="0" result="rimAlpha"/>',
        f'        <feFlood flood-color="white" result="white"/>',
        f'        <feComposite in="white" in2="rimAlpha" operator="in" result="rimLit"/>',

        # --- 3. Subtle top specular (secondary) ---
        f'        <feGaussianBlur in="SourceAlpha" stdDeviation="15" result="bump"/>',
        f'        <feSpecularLighting surfaceScale="6" specularConstant="0.5"',
        f'                            specularExponent="20" lighting-color="white"',
        f'                            in="bump" result="spec">',
        f'          <fePointLight x="512" y="0" z="500"/>',
        f'        </feSpecularLighting>',
        f'        <feComposite in="spec" in2="SourceAlpha" operator="in" result="specClipped"/>',

        # --- 4. Compose: rim over dimmed, then spec over that ---
        f'        <feComposite in="rimLit" in2="dimmed" operator="over" result="withRim"/>',
        f'        <feComposite in="specClipped" in2="withRim" operator="over"/>',

        f'      </filter>',
    ])


# ---------------------------------------------------------------------------
# Main SVG builder
# ---------------------------------------------------------------------------

def _backdrop_blur_filter(group_idx: int, blur_px: float) -> str:
    """Build a backdrop-blur <filter> with Gaussian blur + refraction.

    After blurring the backdrop content, a subtle displacement via
    feTurbulence + feDisplacementMap simulates light bending through
    thick glass.  The turbulence frequency and displacement scale are
    tuned for a 1024×1024 canvas.
    """
    fid = f"blur-g{group_idx}"
    return "\n".join([
        f'      <filter id="{fid}">',
        f'        <feGaussianBlur stdDeviation="{blur_px}" result="blurred"/>',
        # Subtle refraction: fractal noise displaces the blurred backdrop
        f'        <feTurbulence type="fractalNoise" baseFrequency="0.008"',
        f'                      numOctaves="2" seed="1" result="noise"/>',
        f'        <feDisplacementMap in="blurred" in2="noise" scale="10"',
        f'                           xChannelSelector="R" yChannelSelector="G"/>',
        f'      </filter>',
    ])


def _build_layer_elements(
    group: dict,
    g_idx: int,
    colors: ColorRegistry,
    defs: list[str],
    indent: str,
    bundle_dir: str,
) -> list[str]:
    """Render layer elements for a single group, inlining assets."""
    lines: list[str] = []
    for l_idx, layer in enumerate(group.get("layers", [])):
        image_name = layer.get("image-name", "")
        layer_fill = layer.get("fill")
        layer_opacity = layer.get("opacity")

        if image_name.lower().endswith(".pdf"):
            print(f"warning: skipping PDF asset {image_name}", file=sys.stderr)
            continue

        op_attr = ""
        if layer_opacity is not None and abs(layer_opacity - 1.0) > 0.001:
            op_attr = f' opacity="{round(layer_opacity, 4)}"'

        if layer_fill:
            mask_id = f"mask-g{g_idx}-l{l_idx}"
            grad_id = f"fill-g{g_idx}-l{l_idx}"
            grad_elem, fill_attr = _register_fill(layer_fill, grad_id, colors)
            if grad_elem:
                defs.append(grad_elem)
            mask_content = _inline_asset_element(
                bundle_dir, image_name, "        "
            )
            defs.append(
                f'      <mask id="{mask_id}" style="mask-type: alpha">\n'
                f'{mask_content}\n'
                f'      </mask>'
            )
            lines.append(
                f'{indent}<rect width="1024" height="1024"'
                f' fill="{fill_attr}" mask="url(#{mask_id})"{op_attr}/>'
            )
        else:
            lines.append(
                _inline_asset_element(bundle_dir, image_name, indent, op_attr)
            )
    return lines


def build_svg(doc: dict, bundle_dir: str) -> str:
    """Build the complete SVG string from an icon.json document."""
    colors = ColorRegistry()
    defs: list[str] = []
    body: list[str] = []

    # ---- Background fill ----
    fill = doc.get("fill", {})
    bg_grad, bg_attr = _register_fill(fill, "bg-fill", colors)
    if bg_grad:
        defs.append(bg_grad)
    body.append(f'  <rect width="1024" height="1024" fill="{bg_attr}"/>')

    # ---- Glass-sheen gradient (shared, added once if any group uses specular) ----
    groups = doc.get("groups", [])
    any_specular = any(g.get("specular", True) for g in groups)
    if any_specular:
        defs.append(
            '      <linearGradient id="glass-sheen" gradientUnits="objectBoundingBox"'
            ' x1="0.5" y1="0" x2="0.5" y2="1">\n'
            '        <stop offset="0" stop-color="white" stop-opacity="0.12"/>\n'
            '        <stop offset="0.4" stop-color="white" stop-opacity="0.0"/>\n'
            '        <stop offset="0.6" stop-color="black" stop-opacity="0.0"/>\n'
            '        <stop offset="1" stop-color="black" stop-opacity="0.08"/>\n'
            '      </linearGradient>'
        )

    # ---- Groups ----

    # icon.json is foreground-first; SVG paints later elements on top.
    # Iterate in reverse so the bottom-most group is painted first.
    for rev_i, group in enumerate(reversed(groups)):
        g_idx = len(groups) - 1 - rev_i   # original JSON index (for IDs)

        # -- translucency -> group opacity --
        trans = group.get("translucency", {})
        if trans.get("enabled", False):
            g_opacity = max(0.0, min(1.0, 1.0 - trans.get("value", 0.5)))
        else:
            g_opacity = 1.0
        g_opacity_attr = f' opacity="{g_opacity:.4f}"' if g_opacity < 1.0 else ""

        # -- shadow filter --
        shadow = group.get("shadow", {})
        shadow_kind = shadow.get("kind", "none")
        shadow_opacity = shadow.get("opacity", 1.0)
        shadow_elem = _shadow_filter(g_idx, shadow_kind, shadow_opacity)
        if shadow_elem:
            defs.append(shadow_elem)
        has_shadow = shadow_kind != "none"

        # -- specular filter --
        has_specular = group.get("specular", True)   # absent => True
        if has_specular:
            defs.append(_specular_filter(g_idx))

        # -- backdrop blur --
        # CSS backdrop-filter does not work on SVG elements.  Instead we
        # wrap everything rendered so far in a <g id="backdrop-gN">,
        # then add a <use> referencing it with a Gaussian-blur filter,
        # masked by the group's layer shapes so the blur is only visible
        # through the content silhouette (like frosted glass).
        blur_val = group.get("blur-material", 0)
        has_blur = blur_val and blur_val > 0

        if has_blur:
            backdrop_id = f"backdrop-g{g_idx}"
            blur_mask_id = f"blur-mask-g{g_idx}"

            # Wrap all content emitted so far in a <g> for reference
            body = (
                [f'  <g id="{backdrop_id}">']
                + ["  " + line for line in body]
                + ["  </g>"]
            )

            # Build mask from the group's layer shapes (composite alpha)
            mask_lines = [f'      <mask id="{blur_mask_id}" style="mask-type: alpha">']
            for layer in group.get("layers", []):
                img = layer.get("image-name", "")
                if img.lower().endswith(".pdf"):
                    continue
                lo = layer.get("opacity")
                lo_attr = ""
                if lo is not None and abs(lo - 1.0) > 0.001:
                    lo_attr = f' opacity="{round(lo, 4)}"'
                mask_lines.append(
                    _inline_asset_element(bundle_dir, img, "        ", lo_attr)
                )
            mask_lines.append("      </mask>")
            defs.append("\n".join(mask_lines))

            # Add blur filter
            blur_px = round(blur_val * 100, 2)
            defs.append(_backdrop_blur_filter(g_idx, blur_px))

            # Blurred copy, masked to layer shapes
            body.append(
                f'  <use href="#{backdrop_id}"'
                f' filter="url(#blur-g{g_idx})"'
                f' mask="url(#{blur_mask_id})"/>'
            )

        # -- group content (layers wrapped in shadow / specular filters) --
        group_lines: list[str] = []
        indent = "    "

        if has_shadow:
            group_lines.append(f'{indent}<g filter="url(#shadow-g{g_idx})">')
            indent += "  "
        if has_specular:
            group_lines.append(f'{indent}<g filter="url(#specular-g{g_idx})">')
            indent += "  "

        group_lines.extend(
            _build_layer_elements(group, g_idx, colors, defs, indent, bundle_dir)
        )

        if has_specular:
            indent = indent[:-2]
            group_lines.append(f'{indent}</g>')
        if has_shadow:
            indent = indent[:-2]
            group_lines.append(f'{indent}</g>')

        # -- glass sheen overlay (gradient masked to layer shapes) --
        if has_specular:
            glass_mask_id = f"glass-mask-g{g_idx}"
            mask_lines = [f'      <mask id="{glass_mask_id}" style="mask-type: alpha">']
            for layer in group.get("layers", []):
                img = layer.get("image-name", "")
                if img.lower().endswith(".pdf"):
                    continue
                mask_lines.append(
                    _inline_asset_element(bundle_dir, img, "        ")
                )
            mask_lines.append("      </mask>")
            defs.append("\n".join(mask_lines))
            group_lines.append(
                f'    <rect width="1024" height="1024"'
                f' fill="url(#glass-sheen)" mask="url(#{glass_mask_id})"/>'
            )

        # Wrap in a <g> with translucency
        body.append(f'  <g{g_opacity_attr}>')
        body.extend(group_lines)
        body.append("  </g>")

    # ---- Assemble ----
    css = colors.css_block()

    svg: list[str] = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">']
    if css:
        svg.append(css)
    if defs:
        svg.append("  <defs>")
        svg.extend(defs)
        svg.append("  </defs>")
    svg.extend(body)
    svg.append("</svg>")
    svg.append("")  # trailing newline
    return "\n".join(svg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: icon2svg.py <bundle.icon>", file=sys.stderr)
        sys.exit(1)

    bundle_dir = sys.argv[1].rstrip("/")
    if not os.path.isdir(bundle_dir):
        print(f"Error: not a directory: {bundle_dir}", file=sys.stderr)
        sys.exit(1)

    icon_json_path = os.path.join(bundle_dir, "icon.json")
    if not os.path.isfile(icon_json_path):
        print(f"Error: icon.json not found in {bundle_dir}", file=sys.stderr)
        sys.exit(1)

    with open(icon_json_path) as f:
        doc = json.load(f)

    svg = build_svg(doc, bundle_dir)

    out_path = os.path.join(bundle_dir, "icon.svg")
    with open(out_path, "w") as f:
        f.write(svg)

    n_groups = len(doc.get("groups", []))
    n_layers = sum(len(g.get("layers", [])) for g in doc.get("groups", []))
    print(f"Wrote {out_path} ({n_groups} groups, {n_layers} layers)")


if __name__ == "__main__":
    main()
