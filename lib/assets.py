"""
Asset filtering, copying, cleanup, and filename simplification.

Handles extracting the right files from act's output into the .icon bundle's
Assets/ directory, deduplicating variants, and renaming to clean names.
"""

import hashlib
import os
import re
import shutil

from .catalog import (
    collect_groups_from_catalog,
    build_rendition_lookup,
)


# ---------------------------------------------------------------------------
# Layer-to-file matching
# ---------------------------------------------------------------------------

def find_asset_file_for_layer(layer_name: str, icon_name: str, asset_files: list, rendition_lookup: dict[str, str] | None = None) -> str | None:
    """Match a catalog layer name (e.g. AppIcon/1_person or AppIcon) to an actual filename in Assets/."""
    def normalize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("/", "_")

    # --- Primary: match via RenditionName stem from catalog metadata ---
    # The rendition_lookup maps layer Name -> RenditionName stem, which is
    # the authoritative filename that act uses for extracted files.
    if rendition_lookup and layer_name in rendition_lookup:
        rendition_stem = normalize(rendition_lookup[layer_name])
        # Exact rendition match
        for f in asset_files:
            base, _ = os.path.splitext(f)
            if normalize(base) == rendition_stem:
                return f
        # Prefix match (act may insert _Normal between stem and @Nx)
        for f in asset_files:
            base, _ = os.path.splitext(f)
            base_norm = normalize(base)
            if base_norm.startswith(rendition_stem + "_") or base_norm.startswith(rendition_stem.replace("@", "_")):
                return f
        # Also try without @Nx scale suffix
        no_scale = re.sub(r'@\d+x$', '', rendition_stem)
        if no_scale != rendition_stem:
            for f in asset_files:
                base, _ = os.path.splitext(f)
                base_norm = normalize(base)
                if base_norm.startswith(no_scale + "_"):
                    return f

    # --- Fallback: match by layer display name ---
    if layer_name == icon_name:
        # Main app icon image: look for filename containing "AppIcon" or "welcome"
        for f in asset_files:
            base, _ = os.path.splitext(f)
            if "appicon" in normalize(base) or "welcome" in normalize(base):
                return f
        return None

    suffix = layer_name[len(icon_name) + 1:] if layer_name.startswith(icon_name + "/") else layer_name
    norm_suffix = normalize(suffix)

    # Exact name match
    for f in asset_files:
        base, ext = os.path.splitext(f)
        if normalize(base) == norm_suffix:
            return f

    # Prefix/suffix match
    for f in asset_files:
        base, ext = os.path.splitext(f)
        base_norm = normalize(base)
        if base_norm.startswith(norm_suffix + "_") or base_norm.endswith("_" + norm_suffix):
            return f

    return None


# ---------------------------------------------------------------------------
# Filename simplification
# ---------------------------------------------------------------------------

def simplify_asset_filename(display_name: str, current_filename: str) -> str:
    """Return a simple filename for the layer (e.g. 1_person.svg)."""
    _, ext = os.path.splitext(current_filename)
    # Sanitize: avoid path separators and empty
    safe = re.sub(r'[^\w\-.]', "_", display_name).strip("_") or "layer"
    return f"{safe}{ext}"


def resolve_layer_filenames(
    catalog: list,
    icon_name: str,
    assets_dir: str,
    rendition_lookup: dict[str, str] | None = None,
) -> tuple[dict[str, str], list]:
    """Match all layers to asset files and rename to clean names in one pass.

    Calls find_asset_file_for_layer once per layer, renames the matched file
    to a simplified display name, and returns a tuple of:
      - mapping: catalog layer name -> final filename on disk
      - group_specs: the GroupSpec list (so callers don't re-parse the catalog)

    This replaces the old two-step approach (simplify then re-match) that
    was fragile when multiple layers shared the same RenditionName.
    """
    group_specs = collect_groups_from_catalog(catalog, icon_name)
    asset_files = [
        f for f in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, f)) and f.endswith((".png", ".pdf", ".svg"))
    ]

    mapping: dict[str, str] = {}       # layer_name -> final_filename
    used_names: set[str] = set()

    for gs in reversed(group_specs):
        for ls in gs.layers:
            filename = find_asset_file_for_layer(ls.vector_name, icon_name, asset_files, rendition_lookup)
            if not filename:
                continue
            # Compute simplified name from the layer's display name
            simple = simplify_asset_filename(ls.display_name, filename)
            if simple != filename and simple not in used_names:
                src = os.path.join(assets_dir, filename)
                dst = os.path.join(assets_dir, simple)
                if not os.path.exists(dst):
                    os.rename(src, dst)
                    asset_files = [simple if f == filename else f for f in asset_files]
                    filename = simple
            used_names.add(filename)
            mapping[ls.vector_name] = filename

    return mapping, group_specs


# ---------------------------------------------------------------------------
# Asset filtering & copying
# ---------------------------------------------------------------------------

# Regex for act extraction suffixes.  act inserts _Normal or
# _unspecified_unspecified_automatic before any @Nx scale suffix,
# e.g. "icon_512x512_Normal@2x.png".  Group 1 captures the act suffix,
# group 2 preserves the optional @Nx scale.
_ACT_DUPE_SUFFIXES = re.compile(
    r'(_Normal(?:_\d+)?|_unspecified_unspecified_automatic(?:_\d+)?)((?:@\d+x)?)$'
)

_SCALE_SUFFIX = re.compile(r'@\d+x$')
_SCALE_IN_NAME = re.compile(r'@(\d+)x$')


def filter_and_copy_assets(
    catalog: list,
    icon_name: str,
    extracted_dir: str,
    assets_dir: str,
    rendition_lookup: dict[str, str] | None = None,
) -> int:
    """
    Copy only icon-related assets from extracted_dir into assets_dir.
    Uses the same group/layer collection logic as build_icon_composer_doc.
    Returns the number of assets kept.
    """
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

    # Also add RenditionName stems so we match files even when the extracted
    # filename (derived from RenditionName) differs from the catalog layer name.
    # Strip @Nx scale suffixes too, since act inserts _Normal between stem and @Nx.
    if rendition_lookup is None:
        rendition_lookup = build_rendition_lookup(catalog, icon_name)
    # Check if this is a legacy bitmap-only icon (no composable Vector/Image layers)
    has_composable = any(
        isinstance(e, dict) and e.get("AssetType") in ("Vector", "Image")
        for e in catalog[1:]
    )
    # Build set of layer names whose catalog AssetType is "Icon Image"
    # (pre-rendered composites, not individual layer assets).
    icon_image_names: set[str] = set()
    for entry in catalog[1:]:
        if not isinstance(entry, dict):
            continue
        if entry.get("AssetType") == "Icon Image":
            n = entry.get("Name")
            if n:
                icon_image_names.add(n)

    for layer_name_key, stem in rendition_lookup.items():
        # Skip Icon Image renditions for composable apps — they are pre-rendered
        # composite icons, not individual layer assets.
        if has_composable and layer_name_key in icon_image_names:
            continue
        signatures.add(stem)

    def normalize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("/", "_")

    # Normalize away @Nx scale suffixes for matching.  act inserts _Normal
    # between the base stem and @Nx (e.g. icon_512x512_Normal@2x.png), so we
    # strip @Nx from both the signature and the filename before comparing.
    def strip_scale(s: str) -> str:
        return _SCALE_SUFFIX.sub("", s)

    norm_sigs = {strip_scale(normalize(s)) for s in signatures}
    sorted_norms = sorted(norm_sigs, key=len, reverse=True)

    def basename_matches(basename_no_ext: str) -> bool:
        bn = strip_scale(normalize(basename_no_ext))
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

    # Cleanup: deduplicate act extraction variants (_Normal, _Normal_1,
    # _unspecified_unspecified_automatic, etc.).
    # act often extracts multiple copies of the same asset under suffixed names.
    # Group by cleaned base name, then sub-group by file content (size as proxy).
    # Only truly identical files (same size) are deduplicated; files with
    # different content are kept as distinct assets.
    copied_files = [
        f for f in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, f))
    ]
    # Group files by their cleaned base name + extension
    name_groups: dict[str, list[str]] = {}
    for fn in copied_files:
        base, ext = os.path.splitext(fn)
        cleaned = _ACT_DUPE_SUFFIXES.sub(r'\2', base)
        key = cleaned.lower() + ext.lower()
        name_groups.setdefault(key, []).append(fn)

    for key, fns in name_groups.items():
        if len(fns) <= 1:
            continue
        # Sub-group by content hash — only merge truly identical files
        by_hash: dict[str, list[str]] = {}
        for fn in fns:
            h = hashlib.md5(open(os.path.join(assets_dir, fn), "rb").read()).hexdigest()
            by_hash.setdefault(h, []).append(fn)

        if len(by_hash) == 1:
            # All files are identical content — keep one, rename to clean name
            fns.sort(key=len)
            keeper = fns[0]
            for fn in fns[1:]:
                os.remove(os.path.join(assets_dir, fn))
                kept -= 1
            base, ext = os.path.splitext(keeper)
            cleaned = _ACT_DUPE_SUFFIXES.sub(r'\2', base)
            if cleaned != base:
                dst = os.path.join(assets_dir, cleaned + ext)
                if not os.path.exists(dst):
                    os.rename(os.path.join(assets_dir, keeper), dst)
        else:
            # Files have different content — deduplicate within each hash group
            # but keep one representative per unique content
            for h, hash_fns in by_hash.items():
                if len(hash_fns) <= 1:
                    continue
                hash_fns.sort(key=len)
                for fn in hash_fns[1:]:
                    os.remove(os.path.join(assets_dir, fn))
                    kept -= 1

    # Final pass: when multiple @Nx scale variants exist for the same base name
    # (e.g. icon_512x512.png and icon_512x512@2x.png), keep only the highest-res.
    remaining_files = [
        f for f in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, f))
    ]
    scale_groups: dict[str, list[tuple[str, int]]] = {}
    for fn in remaining_files:
        base, ext = os.path.splitext(fn)
        m = _SCALE_IN_NAME.search(base)
        scale = int(m.group(1)) if m else 1
        base_no_scale = _SCALE_IN_NAME.sub("", base)
        key = base_no_scale.lower() + ext.lower()
        scale_groups.setdefault(key, []).append((fn, scale))

    for key, entries in scale_groups.items():
        if len(entries) <= 1:
            continue
        # Keep the highest scale factor
        entries.sort(key=lambda x: x[1], reverse=True)
        for fn, _ in entries[1:]:
            os.remove(os.path.join(assets_dir, fn))
            kept -= 1

    return kept
