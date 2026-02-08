"""
Catalog parsing: color/gradient lookups, group/layer collection, rendition lookup.

Reads assetutil catalog JSON and extracts structured data about icon layers,
appearances, colors, gradients, and rendition names.
"""

import os
import re
import sys


# ---------------------------------------------------------------------------
# Color & gradient helpers
# ---------------------------------------------------------------------------

def _srgb_to_display_p3(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB color components to Display P3 color components.

    Both sRGB and Display P3 use the same transfer function (gamma curve)
    but different primaries.  This converts through CIE XYZ (D65) so that
    the resulting P3 values produce the same visual color as the input
    sRGB values.  Handles extended-range values (outside 0-1).
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

    # 1. Linearize sRGB
    rl, gl, bl = linearize(r), linearize(g), linearize(b)
    # 2. sRGB linear -> XYZ (D65)
    x = 0.4123908 * rl + 0.3575843 * gl + 0.1804808 * bl
    y = 0.2126390 * rl + 0.7151687 * gl + 0.0721923 * bl
    z = 0.0193308 * rl + 0.1191948 * gl + 0.9505322 * bl
    # 3. XYZ (D65) -> Display P3 linear
    p3r =  2.4934969 * x - 0.9313836 * y - 0.4027108 * z
    p3g = -0.8294890 * x + 1.7626641 * y + 0.0236247 * z
    p3b =  0.0358458 * x - 0.0761724 * y + 0.9568845 * z
    # 4. Apply P3 gamma
    return gamma_encode(p3r), gamma_encode(p3g), gamma_encode(p3b)


def color_components_to_string(components: list, colorspace: str) -> str:
    """Format color for Icon Composer (display-p3 or extended-gray).

    When the catalog color is in sRGB, the components are converted to
    Display P3 so that Icon Composer renders the intended visual color.
    """
    if not components:
        return "display-p3:0.5,0.5,0.5,1.0"
    space = (colorspace or "").lower()
    if "gray" in space or len(components) == 2:
        g = components[0]
        a = components[1] if len(components) > 1 else 1.0
        return f"extended-gray:{g:.5f},{a:.5f}"
    if len(components) < 3:
        return "display-p3:0.5,0.5,0.5,1.0"
    r, g, b = components[0], components[1], components[2]
    a = components[3] if len(components) > 3 else 1.0
    # Convert sRGB / extended sRGB to Display P3
    if "srgb" in space:
        r, g, b = _srgb_to_display_p3(r, g, b)
    return f"display-p3:{r:.5f},{g:.5f},{b:.5f},{a:.5f}"


def build_color_lookup(catalog: list, icon_name: str) -> dict:
    """Map catalog Name -> (components, colorspace) for Color entries."""
    lookup = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "Color":
            continue
        name = entry.get("Name")
        if not name:
            continue
        comp = entry.get("Color components")
        space = entry.get("Colorspace", "srgb")
        if comp is not None:
            lookup[name] = (comp, space)
    return lookup


def parse_gradient_start_stop(s: str) -> dict | None:
    """Parse '0.500,0.000 - 0.500,1.000' -> orientation with start/stop x,y."""
    if not s or " - " not in s:
        return None
    parts = s.split(" - ", 1)
    try:
        start_part = parts[0].strip().split(",")
        stop_part = parts[1].strip().split(",")
        return {
            "orientation": {
                "start": {"x": float(start_part[0]), "y": float(start_part[1])},
                "stop": {"x": float(stop_part[0]), "y": float(stop_part[1])},
            }
        }
    except (IndexError, ValueError):
        return None


def build_gradient_lookup(catalog: list) -> dict:
    """Map Named Gradient name -> (list of color names, orientation or None)."""
    lookup = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "Named Gradient":
            continue
        name = entry.get("Name")
        if not name:
            continue
        colors = entry.get("Gradient Colors") or []
        start_stop = entry.get("Gradient Start/Stop")
        parsed = parse_gradient_start_stop(start_stop)
        orientation = parsed["orientation"] if parsed else None
        lookup[name] = (colors, orientation)
    return lookup


def resolve_gradient_to_fill(gradient_name: str, color_lookup: dict, gradient_lookup: dict) -> dict | None:
    """Resolve a Named Gradient to an Icon Composer fill dict."""
    info = gradient_lookup.get(gradient_name)
    if not info:
        return None
    color_names, orientation = info
    if not color_names:
        return None
    resolved = []
    for cn in color_names:
        if cn not in color_lookup:
            break
        comp, space = color_lookup[cn]
        resolved.append(color_components_to_string(comp, space))
    if not resolved:
        return None
    if len(resolved) == 1:
        # Single-color gradient (solid fill) — duplicate to form a valid 2-stop gradient
        resolved = [resolved[0], resolved[0]]
    fill: dict = {"linear-gradient": resolved}
    if orientation:
        fill["orientation"] = orientation
    return fill


def is_gray_gradient(gradient_name: str, color_lookup: dict, gradient_lookup: dict) -> bool:
    """Return True if all colors in the gradient are gray/extended-gray."""
    info = gradient_lookup.get(gradient_name)
    if not info:
        return False
    color_names, _ = info
    for cn in color_names:
        if cn not in color_lookup:
            return False
        comp, space = color_lookup[cn]
        if "gray" in (space or "").lower() or len(comp) == 2:
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Appearance constants
# ---------------------------------------------------------------------------

# Map catalog appearance strings to Icon Composer appearance names.
# Entries not in this map are treated as the default (Light) appearance.
APPEARANCE_MAP = {
    "UIAppearanceDark": "dark",
    "NSAppearanceNameDarkAqua": "dark",
    "ISAppearanceTintable": "tinted",
}

# Appearance strings that represent the Light/default appearance
LIGHT_APPEARANCES = {
    "UIAppearanceLight",
    "UIAppearanceAny",
    "NSAppearanceNameAqua",
}


# ---------------------------------------------------------------------------
# Shadow style mapping
# ---------------------------------------------------------------------------

# Map catalog LayerShadowStyle int -> Icon Composer shadow kind string.
# Confirmed: 3 -> "neutral" (default for Apple's built-in icons)
# Assumed (based on Icon Composer's dropdown order: Off / Neutral / Chromatic):
SHADOW_STYLE_MAP = {
    0: "none",
    1: "neutral",
    2: "layer-color",   # "Chromatic" in Icon Composer UI
    3: "neutral",
}
SHADOW_STYLE_CONFIRMED = {2, 3}  # verified against Icon Composer output


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class LayerSpec:
    """Per-layer info: vector name, fill refs and opacities per appearance."""
    def __init__(self, vector_name: str):
        self.vector_name = vector_name
        self.default_fill_ref: str | None = None
        self.default_opacity: float = 1.0
        self.fill_specializations: dict[str, str] = {}   # appearance -> fill_ref (non-None only)
        self.opacity_specializations: dict[str, float] = {}     # appearance -> opacity
        self.layer_position: str | None = None  # "x,y" from catalog (canvas-relative)
        self.layer_size: str | None = None      # "w,h" from catalog (display size)

    @property
    def display_name(self) -> str:
        """Layer display name: the last path segment of vector_name."""
        return self.vector_name.rsplit("/", 1)[-1]


class GroupSpec:
    """One Icon Composer group: layers + shadow/translucency/glass/specular/blur metadata."""
    def __init__(self, group_name: str):
        self.group_name = group_name
        self.layers: list[LayerSpec] = []
        self.specular: bool = True
        self.shadow_opacity: float = 1.0
        self.shadow_kind: str = "neutral"
        self.translucency_enabled: bool = True
        self.translucency_value: float = 0.5
        self.blur_strength: float | None = None
        self.image_only: bool = False  # True when group has only Image layers (no Vectors)


# ---------------------------------------------------------------------------
# Group & layer collection
# ---------------------------------------------------------------------------

def get_canvas_size(catalog: list) -> tuple[int, int]:
    """Get canvas size from the first IconImageStack entry (default 1024x1024)."""
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconImageStack":
            continue
        w = entry.get("CanvasWidth", 1024)
        h = entry.get("CanvasHeight", 1024)
        return (int(w), int(h))
    return (1024, 1024)


def collect_groups_from_catalog(catalog: list, icon_name: str) -> list[GroupSpec]:
    """
    Collect ordered GroupSpecs from the first IconImageStack.
    Each IconGroup in the stack becomes its own GroupSpec with glass/shadow/translucency
    and per-appearance fill refs and opacities for its vector layer(s).
    """
    # Collect per-group, per-appearance layer info (both Vector and Image assets):
    # group_name -> { appearance -> [(layer_name, fill_ref, opacity)] }
    group_appearance_layers: dict[str, dict[str, list[tuple[str, str | None, float]]]] = {}
    # Track which groups have only Image layers (no Vectors)
    group_has_vector: dict[str, bool] = {}
    # Layer geometry: maps layer name -> (LayerPosition, LayerSize) from catalog
    layer_geometry: dict[str, tuple[str | None, str | None]] = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconGroup":
            continue
        gname = entry.get("Name")
        if not gname:
            continue
        appearance = entry.get("Appearance", "UIAppearanceAny")
        layer_entries = []
        for inner in entry.get("Layers") or []:
            if not isinstance(inner, dict):
                continue
            at = inner.get("AssetType")
            if at in ("Vector", "Image"):
                vn = inner.get("Name")
                if vn:
                    fill_ref = inner.get("LayerGradientColorName")
                    opacity = inner.get("LayerOpacity", 1.0)
                    layer_entries.append((vn, fill_ref, opacity))
                    if at == "Vector":
                        group_has_vector[gname] = True
                    # Collect layer geometry (position/size within canvas)
                    if vn not in layer_geometry:
                        position = inner.get("LayerPosition")
                        size = inner.get("LayerSize")
                        layer_geometry[vn] = (position, size)
        if layer_entries:
            group_appearance_layers.setdefault(gname, {})[appearance] = layer_entries
            group_has_vector.setdefault(gname, False)

    def build_layer_specs(gname: str) -> list[LayerSpec]:
        """Merge all appearances for a group into LayerSpecs."""
        app_data = group_appearance_layers.get(gname, {})
        if not app_data:
            return []
        # Pick canonical appearance: prefer light/default over dark/tinted,
        # since Icon Composer renders the default appearance.
        first_vecs = None
        for app_key in app_data:
            if app_key in LIGHT_APPEARANCES or APPEARANCE_MAP.get(app_key) is None:
                first_vecs = app_data[app_key]
                break
        if first_vecs is None:
            first_vecs = next(iter(app_data.values()))
        specs: list[LayerSpec] = []
        for i, (vn, _, _) in enumerate(first_vecs):
            ls = LayerSpec(vn)
            # Set layer geometry if available
            if vn in layer_geometry:
                ls.layer_position, ls.layer_size = layer_geometry[vn]
            # Pass 1: set defaults from Light/Any appearance
            for appearance, vecs in app_data.items():
                if APPEARANCE_MAP.get(appearance) is not None:
                    continue  # skip non-default appearances
                if i >= len(vecs):
                    continue
                _, fill_ref, opacity = vecs[i]
                ls.default_fill_ref = fill_ref
                ls.default_opacity = opacity
            # Pass 2: add specializations for dark/tinted
            for appearance, vecs in app_data.items():
                ic_appearance = APPEARANCE_MAP.get(appearance)
                if ic_appearance is None:
                    continue
                if i >= len(vecs):
                    continue
                _, fill_ref, opacity = vecs[i]
                if fill_ref is not None:
                    ls.fill_specializations[ic_appearance] = fill_ref
                if abs(opacity - ls.default_opacity) > 0.001:
                    ls.opacity_specializations[ic_appearance] = opacity
            specs.append(ls)
        # Deduplicate identical layers (catalog often stores one entry per
        # size/scale variant, all referencing the same asset with the same opacity).
        deduped: list[LayerSpec] = []
        for ls in specs:
            key = (ls.vector_name, ls.default_fill_ref, ls.default_opacity,
                   tuple(sorted(ls.fill_specializations.items())),
                   tuple(sorted(ls.opacity_specializations.items())))
            if deduped:
                prev = deduped[-1]
                prev_key = (prev.vector_name, prev.default_fill_ref, prev.default_opacity,
                            tuple(sorted(prev.fill_specializations.items())),
                            tuple(sorted(prev.opacity_specializations.items())))
                if key == prev_key:
                    continue  # skip duplicate
            deduped.append(ls)
        return deduped

    # Collect per-group properties from each IconImageStack appearance.
    # Key: (group_name, stack_appearance) -> IconGroup layer dict
    group_props_by_appearance: dict[str, dict[str, dict]] = {}
    stack_order: list[str] = []  # ordered group names from first stack
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconImageStack":
            continue
        stack_appearance = entry.get("Appearance", "UIAppearanceAny")
        for layer in entry.get("Layers") or []:
            if not isinstance(layer, dict) or layer.get("AssetType") != "IconGroup":
                continue
            name = layer.get("Name")
            if not name:
                continue
            group_props_by_appearance.setdefault(name, {})[stack_appearance] = layer
            if not stack_order or name not in stack_order:
                stack_order.append(name)

    groups: list[GroupSpec] = []
    seen_groups: set[str] = set()

    def _read_group_props(gs: GroupSpec, layer: dict) -> None:
        """Populate GroupSpec from an IconGroup's properties."""
        gs.specular = bool(layer.get("LayerHasSpecular") or layer.get("LayerGathersSpecularByElement"))
        shadow_style = layer.get("LayerShadowStyle")
        if shadow_style is not None:
            if shadow_style not in SHADOW_STYLE_MAP:
                print(f"warning: unknown LayerShadowStyle {shadow_style} for group {gs.group_name}, defaulting to 'neutral'", file=sys.stderr)
            elif shadow_style not in SHADOW_STYLE_CONFIRMED:
                print(f"warning: unconfirmed LayerShadowStyle {shadow_style} -> '{SHADOW_STYLE_MAP[shadow_style]}' for group {gs.group_name}", file=sys.stderr)
            gs.shadow_kind = SHADOW_STYLE_MAP.get(shadow_style, "neutral")
            gs.shadow_opacity = layer.get("LayerShadowOpacity", 1.0)
        else:
            # No shadow style key present — use defaults (shadow enabled, neutral)
            gs.shadow_kind = "neutral"
            gs.shadow_opacity = 1.0
        gs.translucency_value = layer.get("LayerTranslucency", 0.5)
        gs.translucency_enabled = gs.translucency_value > 0
        blur = layer.get("LayerBlurStrength")
        if blur and blur > 0:
            gs.blur_strength = blur
        # Image-only groups don't use glass/specular/translucency effects
        if gs.image_only:
            gs.specular = False
            gs.translucency_enabled = False

    for name in stack_order:
        if name in seen_groups:
            continue
        seen_groups.add(name)
        gs = GroupSpec(name)
        gs.layers = build_layer_specs(name)
        gs.image_only = not group_has_vector.get(name, False)

        appearances = group_props_by_appearance.get(name, {})
        # Prefer a Light/default appearance, fall back to first available
        default_layer = None
        for app_name in appearances:
            if app_name in LIGHT_APPEARANCES:
                default_layer = appearances[app_name]
                break
        if default_layer is None:
            # Fall back to first non-dark, non-tinted appearance, then any
            for app_name, layer_dict in appearances.items():
                if APPEARANCE_MAP.get(app_name) is None:
                    default_layer = layer_dict
                    break
            if default_layer is None:
                default_layer = next(iter(appearances.values()), None)
        if default_layer:
            _read_group_props(gs, default_layer)

        # Apply group-level opacity from IconImageStack entries.
        # The LayerOpacity on the IconGroup entry within each stack controls
        # the group's overall visibility for that stack's appearance — separate
        # from the per-layer opacity within the group itself.
        default_group_opacity = 1.0
        group_opacity_specs: dict[str, float] = {}
        for app_name, layer_dict in appearances.items():
            group_op = layer_dict.get("LayerOpacity", 1.0)
            ic_appearance = APPEARANCE_MAP.get(app_name)
            if app_name in LIGHT_APPEARANCES or ic_appearance is None:
                default_group_opacity = group_op
            elif ic_appearance:
                group_opacity_specs[ic_appearance] = group_op

        # Only modify layer opacities if any group opacity differs from 1.0
        has_group_opacity_change = abs(default_group_opacity - 1.0) > 0.001
        if not has_group_opacity_change:
            for go in group_opacity_specs.values():
                if abs(go - 1.0) > 0.001:
                    has_group_opacity_change = True
                    break

        if has_group_opacity_change:
            for ls in gs.layers:
                original_default = ls.default_opacity
                ls.default_opacity = original_default * default_group_opacity
                for appearance, group_op in group_opacity_specs.items():
                    layer_op = ls.opacity_specializations.get(appearance, original_default)
                    effective = layer_op * group_op
                    if abs(effective - ls.default_opacity) > 0.001:
                        ls.opacity_specializations[appearance] = effective
                    elif appearance in ls.opacity_specializations:
                        del ls.opacity_specializations[appearance]

        groups.append(gs)

    # Fallback: no IconImageStack found
    if not groups:
        # Check if the catalog has any Vector or Image entries (composable layers)
        # vs. only "Icon Image" entries (pre-rendered bitmaps from a legacy icon).
        has_composable = False
        for entry in catalog[1:]:
            if not isinstance(entry, dict):
                continue
            if entry.get("AssetType") in ("Vector", "Image"):
                has_composable = True
                break

        if has_composable:
            gs = GroupSpec("default")
            for entry in catalog[1:]:
                if not isinstance(entry, dict):
                    continue
                at = entry.get("AssetType")
                if at in ("Vector", "Image"):
                    n = entry.get("Name")
                    if n:
                        gs.layers.append(LayerSpec(n))
            if gs.layers:
                groups.append(gs)
        else:
            # Legacy bitmap-only icon (only "Icon Image" / "MultiSized Image" entries).
            # Use the highest-resolution bitmap as a single-layer fallback.
            best_entry = None
            best_pixels = 0
            for entry in catalog[1:]:
                if not isinstance(entry, dict) or entry.get("AssetType") != "Icon Image":
                    continue
                pw = entry.get("PixelWidth", 0)
                ph = entry.get("PixelHeight", 0)
                pixels = pw * ph
                if pixels > best_pixels:
                    best_pixels = pixels
                    best_entry = entry
            if best_entry:
                rn = best_entry.get("RenditionName", "")
                name = best_entry.get("Name", icon_name)
                print(f"note: {icon_name} is a legacy bitmap icon — using {rn} ({best_entry.get('PixelWidth')}x{best_entry.get('PixelHeight')}) as single-layer fallback", file=sys.stderr)
                # Create a layer referencing the Icon Image rendition name stem
                gs = GroupSpec("default")
                gs.image_only = True
                gs.specular = False
                gs.translucency_enabled = False
                ls = LayerSpec(name)
                gs.layers.append(ls)
                groups.append(gs)
            else:
                print(f"warning: {icon_name} is a legacy bitmap icon with no usable layers", file=sys.stderr)

    return groups


# ---------------------------------------------------------------------------
# Rendition lookup
# ---------------------------------------------------------------------------

def build_rendition_lookup(catalog: list, icon_name: str) -> dict[str, str]:
    """Map catalog layer Name -> RenditionName stem for Vector/Image/Icon Image entries.

    The stem is the RenditionName without extension (e.g. 'image-left-to-right-base').
    act names extracted files as '{stem}_{attrs}.{ext}', so matching against the stem
    lets us find files even when the layer Name bears no resemblance to the filename.

    When a layer has locale-specific variants (e.g. image-base-la, image-base-ja),
    prefer the Latin variant as the default.
    """
    lookup: dict[str, str] = {}
    # Collect all stems per layer name to detect locale variants
    all_stems: dict[str, list[str]] = {}
    # For Icon Image entries, track pixel count so we prefer the highest resolution
    icon_image_pixels: dict[str, int] = {}
    prefix = icon_name + "/"
    for entry in catalog[1:]:
        if not isinstance(entry, dict):
            continue
        at = entry.get("AssetType")
        if at not in ("Vector", "Image", "Icon Image"):
            continue
        name = entry.get("Name")
        rn = entry.get("RenditionName")
        if not name or not rn:
            continue
        if not name.startswith(prefix) and name != icon_name:
            continue
        stem, _ = os.path.splitext(rn)
        if at == "Icon Image":
            # Prefer the highest-resolution Icon Image entry
            pw = entry.get("PixelWidth", 0)
            ph = entry.get("PixelHeight", 0)
            pixels = pw * ph
            if pixels > icon_image_pixels.get(name, 0):
                icon_image_pixels[name] = pixels
                lookup[name] = stem
        else:
            all_stems.setdefault(name, []).append(stem)

    # For Vector/Image entries, choose the best stem per layer name.
    # If a layer has locale-specific variants (stems ending in -la, -ja, -zh, etc.),
    # prefer the Latin variant (-la) as the default rendering.
    _LOCALE_RE = re.compile(r'-([a-z]{2}(?:-[A-Za-z]+)?)$')
    for name, stems in all_stems.items():
        if name in lookup:
            continue  # already set by Icon Image logic
        unique_stems = list(dict.fromkeys(stems))  # preserve order, deduplicate
        if len(unique_stems) == 1:
            lookup[name] = unique_stems[0]
        else:
            # Check for locale pattern: stems like image-base-la, image-base-ja
            latin_stem = None
            for s in unique_stems:
                m = _LOCALE_RE.search(s)
                if m and m.group(1) == 'la':
                    latin_stem = s
                    break
            if latin_stem:
                lookup[name] = latin_stem
            else:
                lookup[name] = unique_stems[0]
    return lookup
