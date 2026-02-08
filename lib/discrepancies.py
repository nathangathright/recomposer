"""
Discrepancy detection and reporting.

Compares the generated icon.json against the original catalog to identify
assets or features that couldn't be fully represented in Icon Composer format.

Returns structured discrepancy dicts that can be serialized to JSON for
machine consumption or rendered as human-readable text.
"""

import os
import re

from .catalog import APPEARANCE_MAP, LIGHT_APPEARANCES


# ---------------------------------------------------------------------------
# Discrepancy types (used as the "type" field in each dict)
# ---------------------------------------------------------------------------
# bitmap_appearance_variant — dark/tinted bitmap uses a different image file
# orphaned_asset            — file in Assets/ not referenced by icon.json
# unmatched_catalog_layer   — catalog layer with no corresponding asset
# legacy_bitmap_fallback    — icon uses a pre-rendered bitmap (no composable layers)
# locale_variant_unused     — locale-specific glyph not selected as default


def collect_discrepancies(
    catalog: list,
    icon_name: str,
    assets_dir: str,
    doc: dict,
    rendition_lookup: dict[str, str] | None = None,
    layer_filenames: dict[str, str] | None = None,
) -> list[dict]:
    """Detect discrepancies between the catalog and the generated icon.json.

    If layer_filenames is provided (from resolve_layer_filenames), it is used
    to determine which catalog layers were matched, avoiding redundant
    re-matching via find_asset_file_for_layer.

    Returns a list of structured dicts, each with at least:
      - type: str        (one of the type constants above)
      - description: str (human-readable explanation)
    Plus type-specific fields (group, layer, appearance, asset_file, etc.).
    """
    results: list[dict] = []
    prefix = icon_name + "/"

    # Collect all image-name values referenced in the doc
    referenced_images: set[str] = set()
    for g in doc.get("groups", []):
        for layer in g.get("layers", []):
            img = layer.get("image-name")
            if img:
                referenced_images.add(img)

    # --- 1. Bitmap appearance variants not representable ---
    # For each IconGroup, collect per-appearance layer names.
    # If an Image group uses different image files across appearances,
    # the non-default ones are dropped.
    group_appearance_images: dict[str, dict[str, list[str]]] = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "IconGroup":
            continue
        gname = entry.get("Name")
        appearance = entry.get("Appearance", "UIAppearanceAny")
        if not gname:
            continue
        layer_names = []
        for inner in entry.get("Layers") or []:
            if isinstance(inner, dict) and inner.get("AssetType") == "Image":
                n = inner.get("Name")
                if n:
                    layer_names.append(n)
        if layer_names:
            group_appearance_images.setdefault(gname, {})[appearance] = layer_names

    for gname, appearances in group_appearance_images.items():
        if len(appearances) <= 1:
            continue
        # Determine which appearance was used as canonical (light/default preferred)
        canonical_app = None
        for app_key in appearances:
            if app_key in LIGHT_APPEARANCES or APPEARANCE_MAP.get(app_key) is None:
                canonical_app = app_key
                break
        if canonical_app is None:
            canonical_app = next(iter(appearances))
        canonical_names = set(appearances[canonical_app])
        for app_key, layer_names in appearances.items():
            if app_key == canonical_app:
                continue
            ic_app = APPEARANCE_MAP.get(app_key, "default")
            for ln in layer_names:
                if ln not in canonical_names:
                    short = ln[len(prefix):] if ln.startswith(prefix) else ln
                    short_group = gname[len(prefix):] if gname.startswith(prefix) else gname
                    results.append({
                        "type": "bitmap_appearance_variant",
                        "group": short_group,
                        "layer": short,
                        "appearance": ic_app,
                        "description": (
                            f'Group "{short_group}": {ic_app} variant "{short}" not representable '
                            f'in Icon Composer (only one image-name per layer)'
                        ),
                    })

    # --- 2. Orphaned assets ---
    if os.path.isdir(assets_dir):
        asset_files = [
            f for f in os.listdir(assets_dir)
            if os.path.isfile(os.path.join(assets_dir, f)) and f.endswith((".png", ".pdf", ".svg"))
        ]
        orphaned = sorted(f for f in asset_files if f not in referenced_images)
        for f in orphaned:
            results.append({
                "type": "orphaned_asset",
                "asset_file": f,
                "description": f"{f}: present in Assets/ but not referenced by icon.json",
            })

    # --- 3. Unmatched catalog layers ---
    # Catalog layers (Vector/Image) that have no corresponding image-name in the doc
    all_catalog_layers: set[str] = set()
    for entry in catalog[1:]:
        if not isinstance(entry, dict):
            continue
        at = entry.get("AssetType")
        if at in ("Vector", "Image"):
            n = entry.get("Name")
            if n:
                all_catalog_layers.add(n)

    # Build set of catalog layer names that ARE matched in the doc.
    # Use the pre-computed layer_filenames mapping when available,
    # falling back to re-matching only when necessary.
    if layer_filenames is not None:
        matched_catalog_layers = {
            cl for cl in all_catalog_layers
            if cl in layer_filenames and layer_filenames[cl] in referenced_images
        }
    else:
        from .assets import find_asset_file_for_layer
        matched_catalog_layers: set[str] = set()
        if os.path.isdir(assets_dir):
            asset_files = [
                f for f in os.listdir(assets_dir)
                if os.path.isfile(os.path.join(assets_dir, f)) and f.endswith((".png", ".pdf", ".svg"))
            ]
            for cl in all_catalog_layers:
                match = find_asset_file_for_layer(cl, icon_name, asset_files, rendition_lookup)
                if match and match in referenced_images:
                    matched_catalog_layers.add(cl)

    unmatched = sorted(all_catalog_layers - matched_catalog_layers)
    # Filter out layers that are appearance variants (already reported above)
    reported_variants = set()
    for r in results:
        if r["type"] == "bitmap_appearance_variant":
            reported_variants.add(r["layer"])
    truly_unmatched = [
        u for u in unmatched
        if (u[len(prefix):] if u.startswith(prefix) else u) not in reported_variants
    ]
    for u in truly_unmatched:
        short = u[len(prefix):] if u.startswith(prefix) else u
        results.append({
            "type": "unmatched_catalog_layer",
            "layer": short,
            "description": f"{short}: present in catalog but not matched to any asset in icon.json",
        })

    # --- 4. Legacy bitmap fallback ---
    # If the catalog has no composable layers (Vector/Image), only pre-rendered
    # "Icon Image" bitmaps, the icon is a legacy bitmap fallback.
    if not all_catalog_layers:
        has_icon_image = any(
            isinstance(e, dict) and e.get("AssetType") == "Icon Image"
            for e in catalog[1:]
        )
        if has_icon_image:
            results.append({
                "type": "legacy_bitmap_fallback",
                "description": (
                    f"{icon_name}: icon contains only pre-rendered bitmaps "
                    f"with no composable layers — using highest-resolution "
                    f"bitmap as single-layer fallback"
                ),
            })

    # --- 5. Locale variant unused ---
    # Detect layers with locale-specific RenditionName variants where only
    # the Latin variant was selected (other locales are discarded).
    _locale_re = re.compile(r'-([a-z]{2}(?:-[A-Za-z]+)?)$')
    layer_stems: dict[str, list[str]] = {}
    for entry in catalog[1:]:
        if not isinstance(entry, dict):
            continue
        if entry.get("AssetType") not in ("Vector", "Image"):
            continue
        name = entry.get("Name")
        rn = entry.get("RenditionName")
        if not name or not rn:
            continue
        if not name.startswith(prefix) and name != icon_name:
            continue
        stem, _ = os.path.splitext(rn)
        layer_stems.setdefault(name, []).append(stem)

    for name, stems in layer_stems.items():
        unique_stems = list(dict.fromkeys(stems))
        if len(unique_stems) <= 1:
            continue
        locales_found: list[str] = []
        for s in unique_stems:
            m = _locale_re.search(s)
            if m:
                locales_found.append(m.group(1))
        if len(locales_found) >= 2:
            short = name[len(prefix):] if name.startswith(prefix) else name
            non_latin = [loc for loc in locales_found if loc != "la"]
            results.append({
                "type": "locale_variant_unused",
                "description": (
                    f"{short}: locale-specific glyph variant(s) "
                    f"({', '.join(non_latin)}) exist but were not selected "
                    f"— Latin variant preferred"
                ),
            })

    return results
