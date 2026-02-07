#!/usr/bin/env python3
"""
Convert assetutil catalog JSON to Icon Composer document format.
Reads catalog.json (or icon.json) and Assets/ from a .icon bundle directory,
writes Icon Composer-compatible icon.json so the bundle can be opened in Icon Composer.
"""

import json
import os
import re
import sys


def color_components_to_string(components: list, colorspace: str) -> str:
    """Format color for Icon Composer (display-p3 or extended-gray)."""
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
    # Icon Composer uses display-p3 for all RGB colors
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


def _resolve_gradient_to_fill(gradient_name: str, color_lookup: dict, gradient_lookup: dict) -> dict | None:
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
    if len(resolved) < 2:
        return None
    fill: dict = {"linear-gradient": resolved}
    if orientation:
        fill["orientation"] = orientation
    return fill


def _is_gray_gradient(gradient_name: str, color_lookup: dict, gradient_lookup: dict) -> bool:
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


def get_fill_from_catalog(
    catalog: list, icon_name: str, color_lookup: dict, gradient_lookup: dict
) -> tuple[dict, list[dict]]:
    """
    Derive Icon Composer root fill and fill-specializations.

    Identifies background gradients by finding Named Gradients that are NOT
    referenced by any layer's LayerGradientColorName. Gray gradients become
    the tinted specialization; RGB gradients become the default fill.

    Returns (fill_dict, fill_specializations_list).
    """
    # Collect all gradient names referenced by layer fills across all appearances
    layer_fill_refs: set[str] = set()
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconGroup":
            continue
        for inner in entry.get("Layers") or []:
            if isinstance(inner, dict):
                ref = inner.get("LayerGradientColorName")
                if ref:
                    layer_fill_refs.add(ref)

    # Find unreferenced gradients — these are background fills
    prefix = icon_name + "/"
    unreferenced: list[str] = []
    for name in gradient_lookup:
        if name.startswith(prefix) and name not in layer_fill_refs:
            unreferenced.append(name)

    # Classify: RGB gradients -> default fill, gray gradients -> tinted specialization
    default_fill: dict | None = None
    fill_specializations: list[dict] = []
    for gname in unreferenced:
        resolved = _resolve_gradient_to_fill(gname, color_lookup, gradient_lookup)
        if not resolved:
            continue
        if _is_gray_gradient(gname, color_lookup, gradient_lookup):
            fill_specializations.append({"appearance": "tinted", "value": resolved})
        elif default_fill is None:
            default_fill = resolved

    if default_fill:
        return default_fill, fill_specializations

    # Fallback: first Named Gradient for icon
    for name in gradient_lookup:
        if name.startswith(prefix):
            resolved = _resolve_gradient_to_fill(name, color_lookup, gradient_lookup)
            if resolved:
                return resolved, fill_specializations

    # Fallback: first color as automatic-gradient
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "Color":
            continue
        name = entry.get("Name")
        if not name:
            continue
        comp = entry.get("Color components")
        space = entry.get("Colorspace", "srgb")
        if comp is not None:
            return {"automatic-gradient": color_components_to_string(comp, space)}, []
    return {"automatic-gradient": "display-p3:0.5,0.5,0.5,1.00000"}, []


# Map catalog appearance strings to Icon Composer appearance names.
# Entries not in this map are treated as the default (Light) appearance.
_APPEARANCE_MAP = {
    "UIAppearanceDark": "dark",
    "NSAppearanceNameDarkAqua": "dark",
    "ISAppearanceTintable": "tinted",
}

# Appearance strings that represent the Light/default appearance
_LIGHT_APPEARANCES = {
    "UIAppearanceLight",
    "UIAppearanceAny",
    "NSAppearanceNameAqua",
}


class LayerSpec:
    """Per-layer info: vector name, fill refs and opacities per appearance."""
    def __init__(self, vector_name: str):
        self.vector_name = vector_name
        self.default_fill_ref: str | None = None
        self.default_opacity: float = 1.0
        self.fill_specializations: dict[str, str | None] = {}   # appearance -> fill_ref
        self.opacity_specializations: dict[str, float] = {}     # appearance -> opacity


# Map catalog LayerShadowStyle int -> Icon Composer shadow kind string.
# Confirmed: 3 -> "neutral" (default for Apple's built-in icons)
# Assumed (based on Icon Composer's dropdown order: Off / Neutral / Chromatic):
_SHADOW_STYLE_MAP = {
    0: "none",
    1: "neutral",
    2: "layer-color",   # "Chromatic" in Icon Composer UI
    3: "neutral",
}
_SHADOW_STYLE_CONFIRMED = {3}  # only these values have been verified


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
        if layer_entries:
            group_appearance_layers.setdefault(gname, {})[appearance] = layer_entries
            group_has_vector.setdefault(gname, False)

    def build_layer_specs(gname: str) -> list[LayerSpec]:
        """Merge all appearances for a group into LayerSpecs."""
        app_data = group_appearance_layers.get(gname, {})
        if not app_data:
            return []
        # Use first appearance's vector list to determine layer count and names
        first_vecs = next(iter(app_data.values()))
        specs: list[LayerSpec] = []
        for i, (vn, _, _) in enumerate(first_vecs):
            ls = LayerSpec(vn)
            # Pass 1: set defaults from Light/Any appearance
            for appearance, vecs in app_data.items():
                if _APPEARANCE_MAP.get(appearance) is not None:
                    continue  # skip non-default appearances
                if i >= len(vecs):
                    continue
                _, fill_ref, opacity = vecs[i]
                ls.default_fill_ref = fill_ref
                ls.default_opacity = opacity
            # Pass 2: add specializations for dark/tinted
            for appearance, vecs in app_data.items():
                ic_appearance = _APPEARANCE_MAP.get(appearance)
                if ic_appearance is None:
                    continue
                if i >= len(vecs):
                    continue
                _, fill_ref, opacity = vecs[i]
                ls.fill_specializations[ic_appearance] = fill_ref
                if abs(opacity - ls.default_opacity) > 0.001:
                    ls.opacity_specializations[ic_appearance] = opacity
            specs.append(ls)
        return specs

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
            if shadow_style not in _SHADOW_STYLE_MAP:
                print(f"warning: unknown LayerShadowStyle {shadow_style} for group {gs.group_name}, defaulting to 'neutral'", file=sys.stderr)
            elif shadow_style not in _SHADOW_STYLE_CONFIRMED:
                print(f"warning: unconfirmed LayerShadowStyle {shadow_style} -> '{_SHADOW_STYLE_MAP[shadow_style]}' for group {gs.group_name}", file=sys.stderr)
            gs.shadow_kind = _SHADOW_STYLE_MAP.get(shadow_style, "neutral")
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
            if app_name in _LIGHT_APPEARANCES:
                default_layer = appearances[app_name]
                break
        if default_layer is None:
            # Fall back to first non-dark, non-tinted appearance, then any
            for app_name, layer_dict in appearances.items():
                if _APPEARANCE_MAP.get(app_name) is None:
                    default_layer = layer_dict
                    break
            if default_layer is None:
                default_layer = next(iter(appearances.values()), None)
        if default_layer:
            _read_group_props(gs, default_layer)

        groups.append(gs)

    # Fallback: no IconImageStack found
    if not groups:
        gs = GroupSpec("default")
        for entry in catalog[1:]:
            if not isinstance(entry, dict):
                continue
            at = entry.get("AssetType")
            if at in ("Vector", "Image", "Icon Image"):
                n = entry.get("Name")
                if n:
                    gs.layers.append(LayerSpec(n))
        if gs.layers:
            groups.append(gs)

    return groups


def resolve_fill_ref_to_layer_fill(
    fill_ref: str | None,
    icon_name: str,
    color_lookup: dict,
    gradient_lookup: dict,
) -> dict | None:
    """Turn catalog fill_ref (Color or Named Gradient name) into Icon Composer layer fill (linear-gradient + orientation)."""
    if not fill_ref:
        return None
    if fill_ref in color_lookup:
        comp, space = color_lookup[fill_ref]
        cstr = color_components_to_string(comp, space)
        return {"linear-gradient": [cstr, cstr], "orientation": {"start": {"x": 0.5, "y": 0.5}, "stop": {"x": 0.5, "y": 1}}}
    if fill_ref in gradient_lookup:
        color_names, orientation = gradient_lookup[fill_ref]
        resolved = []
        for cn in color_names:
            if cn not in color_lookup:
                break
            comp, space = color_lookup[cn]
            resolved.append(color_components_to_string(comp, space))
        if len(resolved) >= 2:
            fill = {"linear-gradient": resolved}
            if orientation:
                fill["orientation"] = orientation
            return fill
    return None


def find_asset_file_for_layer(layer_name: str, icon_name: str, asset_files: list) -> str | None:
    """Match a catalog layer name (e.g. AppIcon/1_person or AppIcon) to an actual filename in Assets/."""
    def normalize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("/", "_")

    if layer_name == icon_name:
        # Main app icon image: look for filename containing "AppIcon" or "welcome"
        for f in asset_files:
            base, _ = os.path.splitext(f)
            if "appicon" in normalize(base) or "welcome" in normalize(base):
                return f
        return None

    suffix = layer_name[len(icon_name) + 1:] if layer_name.startswith(icon_name + "/") else layer_name
    norm_suffix = normalize(suffix)
    for f in asset_files:
        base, ext = os.path.splitext(f)
        base_norm = normalize(base)
        if norm_suffix == base_norm or base_norm.startswith(norm_suffix + "_") or base_norm.endswith("_" + norm_suffix):
            return f
    return None


def simplify_asset_filename(display_name: str, current_filename: str) -> str:
    """Return a simple filename for the layer (e.g. 1_person.svg)."""
    _, ext = os.path.splitext(current_filename)
    # Sanitize: avoid path separators and empty
    safe = re.sub(r'[^\w\-.]', "_", display_name).strip("_") or "layer"
    return f"{safe}{ext}"


def simplify_asset_filenames_on_disk(
    catalog: list,
    icon_name: str,
    assets_dir: str,
) -> None:
    """Rename asset files on disk to simplified names (e.g. '1_person.svg').

    Must be called before build_icon_composer_doc so filenames match.
    """
    group_specs = collect_groups_from_catalog(catalog, icon_name)
    asset_files = [
        f for f in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, f)) and f.endswith((".png", ".pdf", ".svg"))
    ]
    used_simple_names: set[str] = set()
    for gs in reversed(group_specs):
        for ls in gs.layers:
            filename = find_asset_file_for_layer(ls.vector_name, icon_name, asset_files)
            if not filename:
                continue
            display_name = ls.vector_name.split("/")[-1] if "/" in ls.vector_name else ls.vector_name
            simple = simplify_asset_filename(display_name, filename)
            if simple != filename and simple not in used_simple_names:
                src = os.path.join(assets_dir, filename)
                dst = os.path.join(assets_dir, simple)
                if not os.path.exists(dst):
                    os.rename(src, dst)
                    asset_files = [f if f != filename else simple for f in asset_files]
            used_simple_names.add(simple if simple not in used_simple_names else filename)


def build_icon_composer_doc(
    catalog: list,
    icon_name: str,
    assets_dir: str,
    color_lookup: dict,
    gradient_lookup: dict,
) -> dict:
    """Build Icon Composer-format document with one group per catalog IconGroup.

    Note: call simplify_asset_filenames_on_disk() before this if you want
    simplified filenames — this function is now purely a data transformation.
    """
    fill, fill_specializations = get_fill_from_catalog(catalog, icon_name, color_lookup, gradient_lookup)
    group_specs = collect_groups_from_catalog(catalog, icon_name)

    asset_files = [
        f for f in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, f)) and f.endswith((".png", ".pdf", ".svg"))
    ]

    groups = []
    total_layers = 0

    # Catalog orders groups top-to-bottom (foreground first); Icon Composer stacks bottom-to-top, so reverse.
    for gs in reversed(group_specs):
        layers = []
        for ls in gs.layers:
            filename = find_asset_file_for_layer(ls.vector_name, icon_name, asset_files)
            if not filename:
                continue
            display_name = ls.vector_name.split("/")[-1] if "/" in ls.vector_name else ls.vector_name
            layer: dict = {
                "image-name": filename,
                "name": display_name,
                "glass": gs.specular,
            }
            # Default fill (from Light/Any appearance)
            default_fill = resolve_fill_ref_to_layer_fill(ls.default_fill_ref, icon_name, color_lookup, gradient_lookup)
            if default_fill:
                layer["fill"] = default_fill
            # Fill specializations (dark, tinted)
            fill_specs = []
            for appearance, fill_ref in ls.fill_specializations.items():
                resolved = resolve_fill_ref_to_layer_fill(fill_ref, icon_name, color_lookup, gradient_lookup)
                if resolved:
                    spec: dict = {"appearance": appearance, "value": resolved}
                    fill_specs.append(spec)
                elif fill_ref is None and default_fill:
                    # Appearance has no fill but default does — no specialization needed
                    pass
            if fill_specs:
                layer["fill-specializations"] = fill_specs
            # Opacity specializations (dark, tinted)
            opacity_specs = []
            if abs(ls.default_opacity - 1.0) > 0.001:
                # Non-default default opacity: set it on the layer
                layer["opacity"] = ls.default_opacity
            for appearance, opacity in ls.opacity_specializations.items():
                opacity_specs.append({"appearance": appearance, "value": opacity})
            if opacity_specs:
                layer["opacity-specializations"] = opacity_specs
            layers.append(layer)

        if not layers:
            continue

        group: dict = {"layers": layers}
        if gs.blur_strength is not None and gs.blur_strength > 0:
            group["blur-material"] = round(gs.blur_strength, 5)
        if gs.image_only:
            group["lighting"] = "individual"
        group["shadow"] = {"kind": gs.shadow_kind, "opacity": gs.shadow_opacity}
        if not gs.specular:
            group["specular"] = False
        group["translucency"] = {"enabled": gs.translucency_enabled, "value": gs.translucency_value}
        groups.append(group)
        total_layers += len(layers)

    doc: dict = {
        "fill": fill,
        "groups": groups,
        "supported-platforms": {"circles": ["watchOS"], "squares": "shared"},
    }
    if fill_specializations:
        doc["fill-specializations"] = fill_specializations
    return doc


def _parse_color_string(s: str) -> tuple[float, float, float, float] | None:
    """Parse 'display-p3:r,g,b,a' or 'srgb:r,g,b,a' or 'extended-gray:g,a' -> (r,g,b,a) or (g,g,g,a)."""
    if ":" not in s:
        return None
    try:
        _, rest = s.split(":", 1)
        parts = [float(x.strip()) for x in rest.split(",")]
    except ValueError:
        return None
    if len(parts) == 4:
        return (parts[0], parts[1], parts[2], parts[3])
    if len(parts) == 2:
        return (parts[0], parts[0], parts[0], parts[1])
    return None


def _color_distance(c1: tuple[float, ...], c2: tuple[float, ...]) -> float:
    """Rough perceptual distance (0 = same, 1+ = very different)."""
    if not c1 or not c2 or len(c1) != 4 or len(c2) != 4:
        return 1.0
    return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2 + 0.25 * (c1[3] - c2[3]) ** 2) ** 0.5


def score_catalog_fidelity(
    catalog: list,
    icon_name: str,
    doc: dict,
    color_lookup: dict,
    gradient_lookup: dict,
) -> tuple[float, list[str]]:
    """
    Compare recreated icon.json to catalog; return score 0-100 and breakdown lines.
    Weights: root fill (20), layer/group count (15), per-layer fills+specializations (30),
             glass (10), color accuracy (15), opacity accuracy (10).
    """
    breakdown: list[str] = []
    scores: list[float] = []

    # 1) Root fill (20 pts): type, colors, orientation, specializations
    fill = doc.get("fill", {})
    expected_fill, expected_fill_specs = get_fill_from_catalog(catalog, icon_name, color_lookup, gradient_lookup)
    fill_score = 0.0
    if expected_fill.get("linear-gradient"):
        if "linear-gradient" in fill:
            fill_score += 8
            lg = fill["linear-gradient"]
            expected_lg = expected_fill["linear-gradient"]
            if isinstance(lg, list) and len(lg) >= 2:
                fill_score += 4
                # Check color accuracy
                for i in range(min(2, len(lg), len(expected_lg))):
                    p = _parse_color_string(lg[i])
                    e = _parse_color_string(expected_lg[i])
                    if p and e and _color_distance(p, e) < 0.05:
                        fill_score += 1.5
            if "orientation" in fill:
                fill_score += 2
    elif expected_fill:
        if fill:
            fill_score += 15
    # Check root fill-specializations
    expected_spec_count = len(expected_fill_specs)
    actual_specs = doc.get("fill-specializations", [])
    if expected_spec_count > 0:
        actual_spec_appearances = {s.get("appearance") for s in actual_specs}
        expected_spec_appearances = {s.get("appearance") for s in expected_fill_specs}
        matched = len(actual_spec_appearances & expected_spec_appearances)
        fill_score += 3 * (matched / expected_spec_count)
    else:
        fill_score += 3  # no specializations expected, full credit
    scores.append(min(20, fill_score))
    breakdown.append(f"fill: {min(20, fill_score):.0f}/20 (specs: {len(actual_specs)}/{expected_spec_count})")

    # 2) Layer and group count (15 pts)
    group_specs = collect_groups_from_catalog(catalog, icon_name)
    expected_layers = sum(len(gs.layers) for gs in group_specs)
    expected_groups = len(group_specs)
    actual_groups = doc.get("groups", [])
    actual_layers_total = sum(len(g.get("layers", [])) for g in actual_groups)
    actual_groups_count = len(actual_groups)
    layer_score = 0.0
    if expected_layers and actual_layers_total == expected_layers:
        layer_score += 7.5
    elif expected_layers:
        layer_score += 7.5 * (actual_layers_total / expected_layers)
    else:
        layer_score = 7.5
    if expected_groups and actual_groups_count == expected_groups:
        layer_score += 7.5
    elif expected_groups:
        layer_score += 7.5 * (actual_groups_count / expected_groups)
    else:
        layer_score += 7.5
    scores.append(min(15, layer_score))
    breakdown.append(f"groups: {actual_groups_count}/{expected_groups}, layers: {actual_layers_total}/{expected_layers} ({min(15, layer_score):.0f}/15)")

    # 3) Per-layer fills + fill-specializations (30 pts)
    all_layer_specs = [(ls, gs) for gs in group_specs for ls in gs.layers]
    all_actual_layers = [L for g in actual_groups for L in g.get("layers", [])]
    # Fill presence (15 pts)
    layers_with_fill_ref = sum(1 for ls, _ in all_layer_specs if ls.default_fill_ref or ls.fill_specializations)
    layers_with_fill_in_doc = sum(1 for L in all_actual_layers if L.get("fill") or L.get("fill-specializations"))
    fill_pts = 15 * (layers_with_fill_in_doc / layers_with_fill_ref) if layers_with_fill_ref else 15
    # Fill-specialization coverage (15 pts)
    expected_spec_total = sum(len(ls.fill_specializations) for ls, _ in all_layer_specs)
    actual_spec_total = sum(len(L.get("fill-specializations", [])) for L in all_actual_layers)
    spec_pts = 15 * (actual_spec_total / expected_spec_total) if expected_spec_total else 15
    layer_fill_score = fill_pts + spec_pts
    scores.append(min(30, layer_fill_score))
    breakdown.append(f"layer fills: {layers_with_fill_in_doc}/{layers_with_fill_ref}, fill-specs: {actual_spec_total}/{expected_spec_total} ({min(30, layer_fill_score):.0f}/30)")

    # 4) Glass (10 pts)
    layers_expecting_glass = sum(1 for _, gs in all_layer_specs if gs.specular)
    layers_with_glass_in_doc = sum(1 for L in all_actual_layers if L.get("glass"))
    glass_score = 10 * (layers_with_glass_in_doc / layers_expecting_glass) if layers_expecting_glass else 10
    scores.append(min(10, glass_score))
    breakdown.append(f"glass: {layers_with_glass_in_doc}/{layers_expecting_glass} ({min(10, glass_score):.0f}/10)")

    # 5) Color accuracy (15 pts): root fill colors
    color_score = 15.0
    if fill.get("linear-gradient") and expected_fill.get("linear-gradient"):
        lg = fill["linear-gradient"]
        expected_lg = expected_fill["linear-gradient"]
        comparisons = min(2, len(lg), len(expected_lg))
        for i in range(comparisons):
            p = _parse_color_string(lg[i])
            e = _parse_color_string(expected_lg[i])
            if p and e:
                d = _color_distance(p, e)
                color_score -= min(3.75, d * 20)
    scores.append(max(0, color_score))
    breakdown.append(f"color match: {max(0, color_score):.0f}/15")

    # 6) Opacity accuracy (10 pts): default opacity + specializations
    opacity_score = 10.0
    expected_opacity_specs = sum(len(ls.opacity_specializations) for ls, _ in all_layer_specs)
    actual_opacity_specs = sum(len(L.get("opacity-specializations", [])) for L in all_actual_layers)
    if expected_opacity_specs > 0:
        opacity_score = 10 * (min(actual_opacity_specs, expected_opacity_specs) / expected_opacity_specs)
    scores.append(min(10, opacity_score))
    breakdown.append(f"opacity-specs: {actual_opacity_specs}/{expected_opacity_specs} ({min(10, opacity_score):.0f}/10)")

    total = sum(scores)
    return (total, breakdown)


def filter_and_copy_assets(
    catalog: list,
    icon_name: str,
    extracted_dir: str,
    assets_dir: str,
) -> int:
    """
    Copy only icon-related assets from extracted_dir into assets_dir.
    Uses the same group/layer collection logic as build_icon_composer_doc.
    Returns the number of assets kept.
    """
    import shutil

    group_specs = collect_groups_from_catalog(catalog, icon_name)
    # Collect all vector layer names across all groups
    layer_names: list[str] = []
    for gs in group_specs:
        for ls in gs.layers:
            if ls.vector_name not in layer_names:
                layer_names.append(ls.vector_name)

    if not layer_names:
        # Fallback: any Vector, Image, or Icon Image entries
        for entry in catalog[1:]:
            if not isinstance(entry, dict):
                continue
            if entry.get("AssetType") in ("Vector", "Image", "Icon Image") and entry.get("Name"):
                layer_names.append(entry["Name"])

    prefix = icon_name + "/"
    signatures: set[str] = set()
    for n in layer_names:
        signatures.add(n)
        if n.startswith(prefix):
            signatures.add(n[len(prefix):])

    def normalize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("/", "_")

    norm_to_sig = {normalize(s): s for s in signatures}
    sorted_norms = sorted(norm_to_sig.keys(), key=len, reverse=True)

    def basename_matches(basename_no_ext: str) -> bool:
        bn = normalize(basename_no_ext)
        for norm in sorted_norms:
            if norm == bn:
                return True
            if bn.startswith(norm + "_") or bn.endswith("_" + norm):
                return True
            if "_" + norm + "_" in bn:
                return True
        return False

    # Clear existing assets
    for fn in os.listdir(assets_dir):
        fp = os.path.join(assets_dir, fn)
        if os.path.isfile(fp):
            os.remove(fp)

    kept = 0
    for fn in os.listdir(extracted_dir):
        path = os.path.join(extracted_dir, fn)
        if not os.path.isfile(path):
            continue
        base, ext = os.path.splitext(fn)
        if ext.lower() not in (".png", ".pdf", ".svg"):
            continue
        if basename_matches(base):
            shutil.copy2(path, os.path.join(assets_dir, fn))
            kept += 1

    return kept


def _parse_args() -> dict:
    """Parse CLI arguments into a dict of options."""
    args = sys.argv[1:]
    opts: dict = {
        "score_only": False,
        "icon_name": None,
        "extracted_dir": None,
        "bundle_dir": None,
    }
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--score-only":
            opts["score_only"] = True
        elif args[i] == "--icon-name":
            if i + 1 >= len(args):
                print("Error: --icon-name requires a value", file=sys.stderr)
                sys.exit(1)
            i += 1
            opts["icon_name"] = args[i]
        elif args[i] == "--extracted-dir":
            if i + 1 >= len(args):
                print("Error: --extracted-dir requires a value", file=sys.stderr)
                sys.exit(1)
            i += 1
            opts["extracted_dir"] = args[i]
        elif args[i].startswith("--"):
            print(f"Error: unknown option: {args[i]}", file=sys.stderr)
            sys.exit(1)
        else:
            positional.append(args[i])
        i += 1
    if positional:
        opts["bundle_dir"] = positional[0]
    return opts


def main() -> None:
    opts = _parse_args()
    score_only = opts["score_only"]
    icon_name = opts["icon_name"]
    extracted_dir = opts["extracted_dir"]
    bundle_dir = opts["bundle_dir"]

    if not bundle_dir:
        print("Usage: catalog_to_icon_composer.py [OPTIONS] <bundle_dir>", file=sys.stderr)
        print("  bundle_dir: path to .icon directory containing catalog.json and Assets/", file=sys.stderr)
        print("Options:", file=sys.stderr)
        print("  --icon-name NAME      Icon name (default: inferred from catalog)", file=sys.stderr)
        print("  --extracted-dir DIR   Filter and copy assets from DIR into Assets/", file=sys.stderr)
        print("  --score-only          Only print fidelity score for existing icon.json", file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(bundle_dir):
        print(f"Error: not a directory: {bundle_dir}", file=sys.stderr)
        sys.exit(1)

    assets_dir = os.path.join(bundle_dir, "Assets")

    # Load catalog
    catalog_path = os.path.join(bundle_dir, "catalog.json")
    if not os.path.isfile(catalog_path):
        print(f"Error: catalog.json not found in {bundle_dir}", file=sys.stderr)
        sys.exit(1)
    with open(catalog_path) as f:
        catalog = json.load(f)

    if not isinstance(catalog, list) or not catalog or not isinstance(catalog[0], dict):
        print("Error: invalid catalog (empty or no metadata)", file=sys.stderr)
        sys.exit(1)

    if not icon_name:
        print("Error: --icon-name is required", file=sys.stderr)
        sys.exit(1)

    color_lookup = build_color_lookup(catalog, icon_name)
    gradient_lookup = build_gradient_lookup(catalog)

    # Filter and copy assets from extracted dir if provided
    if extracted_dir:
        if not os.path.isdir(extracted_dir):
            print(f"Error: extracted dir not found: {extracted_dir}", file=sys.stderr)
            sys.exit(1)
        os.makedirs(assets_dir, exist_ok=True)
        kept = filter_and_copy_assets(catalog, icon_name, extracted_dir, assets_dir)
        print(f"Kept {kept} icon-related asset(s) in {assets_dir}")

    if score_only:
        icon_path = os.path.join(bundle_dir, "icon.json")
        if not os.path.isfile(icon_path):
            print("Error: icon.json not found (run without --score-only first)", file=sys.stderr)
            sys.exit(1)
        with open(icon_path) as f:
            doc = json.load(f)
        score, breakdown = score_catalog_fidelity(catalog, icon_name, doc, color_lookup, gradient_lookup)
        print(f"Catalog fidelity score: {score:.0f}/100")
        for line in breakdown:
            print(f"  {line}")
        return

    if not os.path.isdir(assets_dir):
        print(f"Error: Assets/ not found in {bundle_dir}", file=sys.stderr)
        sys.exit(1)

    simplify_asset_filenames_on_disk(catalog, icon_name, assets_dir)
    doc = build_icon_composer_doc(catalog, icon_name, assets_dir, color_lookup, gradient_lookup)

    out_path = os.path.join(bundle_dir, "icon.json")
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)

    total_layers = sum(len(g.get("layers", [])) for g in doc.get("groups", []))
    print(f"Wrote Icon Composer icon.json to {out_path} ({len(doc['groups'])} groups, {total_layers} layers)")

    score, breakdown = score_catalog_fidelity(catalog, icon_name, doc, color_lookup, gradient_lookup)
    print(f"Catalog fidelity score: {score:.0f}/100")
    for line in breakdown:
        print(f"  {line}")


if __name__ == "__main__":
    main()
