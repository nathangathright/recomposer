"""
Asset filtering, copying, cleanup, and filename simplification.

Handles extracting the right files from act's output into the .icon bundle's
Assets/ directory, deduplicating variants, and renaming to clean names.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys

from .catalog import (
    collect_groups_from_catalog,
    get_canvas_size,
)


# ---------------------------------------------------------------------------
# Layer-to-file matching
# ---------------------------------------------------------------------------

def find_asset_file_for_layer(layer_name: str, icon_name: str, asset_files: list, rendition_stem: str | None = None) -> str | None:
    """Match a catalog layer name (e.g. AppIcon/1_person or AppIcon) to an actual filename in Assets/."""
    def normalize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("/", "_")

    # --- Primary: match via RenditionName stem from catalog metadata ---
    # The rendition_stem is the authoritative filename stem that act uses
    # for extracted files (provided per-layer from LayerSpec).
    if rendition_stem:
        rendition_stem = normalize(rendition_stem)
        # Exact rendition match
        for f in asset_files:
            base, _ = os.path.splitext(f)
            if normalize(base) == rendition_stem:
                return f
        # Prefix match (act may insert _Normal between stem and @Nx)
        for f in asset_files:
            base, _ = os.path.splitext(f)
            base_norm = normalize(base)
            if base_norm.startswith(rendition_stem + "_") or ("@" in rendition_stem and base_norm.startswith(rendition_stem.replace("@", "_"))):
                return f
        # Also try without @Nx scale suffix
        no_scale = re.sub(r'@\d+x$', '', rendition_stem)
        if no_scale != rendition_stem:
            for f in asset_files:
                base, _ = os.path.splitext(f)
                base_norm = normalize(base)
                if base_norm.startswith(no_scale + "_"):
                    return f

        # --- Variant fallback ---
        # When a catalog layer has a variant suffix (e.g. ".mono", ".watch"),
        # act extracts it as "{base}_unspecified_unspecified_automatic" instead
        # of "{base}.{variant}".  Try stripping the last dotted segment from
        # the rendition stem and matching against _unspecified files.
        dot_idx = rendition_stem.rfind(".")
        if dot_idx > 0:
            stem_base = rendition_stem[:dot_idx]
            uua_suffix = "_unspecified_unspecified_automatic"
            for f in asset_files:
                base, _ = os.path.splitext(f)
                base_norm = normalize(base)
                if base_norm == stem_base + uua_suffix:
                    return f
                # Also match with trailing _N dedup counter
                if base_norm.startswith(stem_base + uua_suffix + "_"):
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
            filename = find_asset_file_for_layer(ls.vector_name, icon_name, asset_files, ls.rendition_stem)
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
    for gs in group_specs:
        for ls in gs.layers:
            if ls.rendition_stem:
                signatures.add(ls.rendition_stem)

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


# ---------------------------------------------------------------------------
# Pre-rendered Icon Image extraction (for scoring reference)
# ---------------------------------------------------------------------------

# Light/default appearances used by pre-rendered Icon Images.
_LIGHT_APPEARANCES = {"", "UIAppearanceAny", "NSAppearanceNameSystem"}


def reframe_assets(
    catalog: list,
    group_specs: list,
    assets_dir: str,
    layer_filenames: dict[str, str],
) -> int:
    """Reframe bitmap assets that are positioned/sized within the canvas.

    When a catalog layer has LayerPosition or LayerSize that don't fill the
    full canvas, the extracted bitmap needs to be resized and repositioned
    within a full-canvas transparent image so Icon Composer renders it correctly.

    Returns the number of assets reframed.
    """
    canvas_w, canvas_h = get_canvas_size(catalog)

    # Find the reframe binary (same directory as this package's parent)
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reframe_bin = os.path.join(script_dir, "reframe")
    if not os.path.isfile(reframe_bin):
        return 0

    reframed = 0
    for gs in group_specs:
        for ls in gs.layers:
            if ls.layer_position is None or ls.layer_size is None:
                continue

            # Parse position and size
            try:
                px, py = [int(v) for v in ls.layer_position.split(",")]
                sw, sh = [int(v) for v in ls.layer_size.split(",")]
            except (ValueError, TypeError):
                continue

            # Skip layers that fill or extend beyond the canvas (bleed/overscan).
            # Only reframe layers that are genuinely inset within the canvas.
            if px < 0 or py < 0:
                continue
            if sw >= canvas_w and sh >= canvas_h:
                continue
            if px == 0 and py == 0 and sw == canvas_w and sh == canvas_h:
                continue

            # Only reframe bitmap files (PNGs), not SVGs
            filename = layer_filenames.get(ls.vector_name)
            if not filename or not filename.lower().endswith(".png"):
                continue

            filepath = os.path.join(assets_dir, filename)
            if not os.path.isfile(filepath):
                continue

            result = subprocess.run(
                [reframe_bin, filepath, filepath,
                 str(canvas_w), str(canvas_h),
                 str(px), str(py), str(sw), str(sh)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                reframed += 1
            else:
                print(f"warning: reframe failed for {filename}: {result.stderr.strip()}", file=sys.stderr)

    return reframed


def find_prerendered_icon(
    catalog: list,
    icon_name: str,
    extracted_dir: str,
) -> str | None:
    """Find the best pre-rendered 1024x1024 Icon Image PNG in the extracted dir.

    The Assets.car contains composited "Icon Image" entries at various sizes.
    We want the highest-resolution default-appearance entry to use as a
    scoring reference.

    Returns the full path to the matched file, or None.
    """
    prefix = icon_name + "/"

    # Collect candidate catalog entries: Icon Image, matching icon name, light appearance
    candidates: list[dict] = []
    for entry in catalog[1:]:
        if not isinstance(entry, dict) or entry.get("AssetType") != "Icon Image":
            continue
        name = entry.get("Name", "")
        if name != icon_name and not name.startswith(prefix):
            continue
        appearance = entry.get("Appearance", "")
        if appearance not in _LIGHT_APPEARANCES:
            continue
        pw = entry.get("PixelWidth", 0)
        ph = entry.get("PixelHeight", 0)
        rn = entry.get("RenditionName", "")
        if pw >= 1024 and ph >= 1024 and rn:
            candidates.append(entry)

    if not candidates:
        return None

    # Prefer scale=1 (the canonical composite render) over scale=2
    candidates.sort(key=lambda e: (e.get("Scale", 1) == 1, e.get("PixelWidth", 0) * e.get("PixelHeight", 0)), reverse=True)
    best = candidates[0]

    # Match the RenditionName stem to an extracted file
    rn = best["RenditionName"]
    stem, _ = os.path.splitext(rn)
    stem_lower = stem.lower()

    for fn in os.listdir(extracted_dir):
        if not fn.lower().endswith(".png"):
            continue
        base, _ = os.path.splitext(fn)
        base_lower = base.lower()
        # Exact stem match, or stem + _Normal suffix (act adds this)
        if base_lower == stem_lower or base_lower.startswith(stem_lower + "_"):
            return os.path.join(extracted_dir, fn)

    return None
