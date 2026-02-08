"""
Icon Composer document builder.

Builds the icon.json document from catalog data, color/gradient lookups,
and extracted asset files.
"""

import os

from .catalog import (
    APPEARANCE_MAP,
    LIGHT_APPEARANCES,
    color_components_to_string,
    collect_groups_from_catalog,
    is_gray_gradient,
    resolve_gradient_to_fill,
)
from .assets import find_asset_file_for_layer


# ---------------------------------------------------------------------------
# Fill resolution
# ---------------------------------------------------------------------------

def get_fill_from_catalog(
    catalog: list, icon_name: str, color_lookup: dict, gradient_lookup: dict
) -> tuple[dict, list[dict]]:
    """
    Derive Icon Composer root fill and fill-specializations.

    Uses IconImageStack entries to determine background gradients per
    appearance.  The light/default stack's background gradient becomes
    the root fill.  The dark stack's background gradient becomes the
    tinted fill-specialization (providing the monochrome base for
    tinted rendering), matching Icon Composer's convention.

    Falls back to heuristic matching when no IconImageStack entries exist.

    Returns (fill_dict, fill_specializations_list).
    """
    prefix = icon_name + "/"

    # --- Primary path: use IconImageStack appearance → gradient mapping ---
    bg_gradient_by_appearance: dict[str, str] = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconImageStack":
            continue
        stack_appearance = entry.get("Appearance", "UIAppearanceAny")
        for layer in entry.get("Layers") or []:
            if not isinstance(layer, dict):
                continue
            # The background gradient is the first non-IconGroup layer
            if layer.get("AssetType") == "IconGroup":
                continue
            name = layer.get("Name")
            if name and name in gradient_lookup:
                bg_gradient_by_appearance.setdefault(stack_appearance, name)
            break  # only inspect the first non-IconGroup layer

    if bg_gradient_by_appearance:
        # Find the light/default background gradient
        light_gradient: str | None = None
        for app_key in bg_gradient_by_appearance:
            if app_key in LIGHT_APPEARANCES:
                light_gradient = bg_gradient_by_appearance[app_key]
                break
        if light_gradient is None:
            # Fall back to first non-dark, non-tinted appearance
            for app_key, gname in bg_gradient_by_appearance.items():
                if APPEARANCE_MAP.get(app_key) is None:
                    light_gradient = gname
                    break

        if light_gradient:
            default_fill = resolve_gradient_to_fill(light_gradient, color_lookup, gradient_lookup)
            if default_fill:
                fill_specializations: list[dict] = []
                # Dark stack's gradient → tinted fill-specialization
                dark_gradient: str | None = None
                for app_key, gname in bg_gradient_by_appearance.items():
                    if APPEARANCE_MAP.get(app_key) == "dark":
                        dark_gradient = gname
                        break
                if dark_gradient and dark_gradient != light_gradient:
                    dark_resolved = resolve_gradient_to_fill(dark_gradient, color_lookup, gradient_lookup)
                    if dark_resolved and dark_resolved != default_fill:
                        fill_specializations.append({"appearance": "tinted", "value": dark_resolved})
                return default_fill, fill_specializations

    # --- Fallback: heuristic for icons without IconImageStack entries ---

    # Collect all gradient names referenced by layer fills
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
    unreferenced: list[str] = []
    for name in gradient_lookup:
        if name.startswith(prefix) and name not in layer_fill_refs:
            unreferenced.append(name)

    # Classify: RGB gradients -> default fill, gray gradients -> tinted specialization
    default_fill = None
    fill_specializations = []
    for gname in unreferenced:
        resolved = resolve_gradient_to_fill(gname, color_lookup, gradient_lookup)
        if not resolved:
            continue
        if is_gray_gradient(gname, color_lookup, gradient_lookup):
            fill_specializations.append({"appearance": "tinted", "value": resolved})
        elif default_fill is None:
            default_fill = resolved

    if default_fill:
        return default_fill, fill_specializations

    # Fallback: first Named Gradient for icon
    for name in gradient_lookup:
        if name.startswith(prefix):
            resolved = resolve_gradient_to_fill(name, color_lookup, gradient_lookup)
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
        if resolved:
            if len(resolved) == 1:
                # Single-color gradient (solid fill) — duplicate to form a valid 2-stop gradient
                resolved = [resolved[0], resolved[0]]
            fill = {"linear-gradient": resolved}
            if orientation:
                fill["orientation"] = orientation
            return fill
    return None


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------

def build_icon_composer_doc(
    catalog: list,
    icon_name: str,
    assets_dir: str,
    color_lookup: dict,
    gradient_lookup: dict,
    rendition_lookup: dict[str, str] | None = None,
    layer_filenames: dict[str, str] | None = None,
    group_specs: list | None = None,
) -> dict:
    """Build Icon Composer-format document with one group per catalog IconGroup.

    If layer_filenames is provided (from resolve_layer_filenames), it is used
    directly instead of re-running find_asset_file_for_layer.  If group_specs
    is also provided, the catalog is not re-parsed for group/layer data.
    """
    fill, fill_specializations = get_fill_from_catalog(catalog, icon_name, color_lookup, gradient_lookup)
    if group_specs is None:
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
            if layer_filenames is not None and ls.vector_name in layer_filenames:
                filename = layer_filenames[ls.vector_name]
            else:
                filename = find_asset_file_for_layer(ls.vector_name, icon_name, asset_files, rendition_lookup)
            if not filename:
                continue
            layer: dict = {
                "image-name": filename,
                "name": ls.display_name,
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

    # Derive supported-platforms from catalog metadata when available.
    # The metadata entry (catalog[0]) contains a "Platform" field:
    #   "macosx"     — macOS-only apps (no watchOS circle support)
    #   "macosx-ios" — multi-platform / Catalyst apps (include watchOS circles)
    supported_platforms: dict = {"squares": "shared"}
    platform = catalog[0].get("Platform", "") if catalog and isinstance(catalog[0], dict) else ""
    if "ios" in platform:
        supported_platforms["circles"] = ["watchOS"]

    doc: dict = {
        "fill": fill,
        "groups": groups,
        "supported-platforms": supported_platforms,
    }
    if fill_specializations:
        doc["fill-specializations"] = fill_specializations
    return doc
