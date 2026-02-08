#!/usr/bin/env python3
"""
Recomposer — convert macOS asset catalog data to Icon Composer format.

CLI entry point. Reads catalog.json and Assets/ from a .icon bundle directory,
writes Icon Composer-compatible icon.json so the bundle can be opened in
Icon Composer.
"""

import json
import os
import shutil
import sys

from lib.catalog import (
    build_color_lookup,
    build_gradient_lookup,
)
from lib.composer import build_icon_composer_doc
from lib.assets import (
    filter_and_copy_assets,
    find_prerendered_icon,
    flatten_svg_groups,
    reframe_assets,
    resolve_layer_filenames,
)
from lib.scoring import score_visual_fidelity
from lib.discrepancies import collect_discrepancies


def _parse_args() -> dict:
    """Parse CLI arguments into a dict of options."""
    args = sys.argv[1:]
    opts: dict = {
        "score_only": False,
        "icon_name": None,
        "extracted_dir": None,
        "app_path": None,
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
        elif args[i] == "--app-path":
            if i + 1 >= len(args):
                print("Error: --app-path requires a value", file=sys.stderr)
                sys.exit(1)
            i += 1
            opts["app_path"] = args[i]
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
    app_path = opts["app_path"]
    bundle_dir = opts["bundle_dir"]

    if not bundle_dir:
        print("Usage: recompose.py [OPTIONS] <bundle_dir>", file=sys.stderr)
        print("  bundle_dir: path to .icon directory containing catalog.json and Assets/", file=sys.stderr)
        print("Options:", file=sys.stderr)
        print("  --icon-name NAME      Icon name (default: inferred from catalog)", file=sys.stderr)
        print("  --extracted-dir DIR   Filter and copy assets from DIR into Assets/", file=sys.stderr)
        print("  --app-path PATH       Path to .app bundle (for visual fidelity scoring)", file=sys.stderr)
        print("  --score-only          Only print visual fidelity score (requires --app-path)", file=sys.stderr)
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

    # Score-only mode: just compare existing .icon to .app visually
    if score_only:
        if not app_path:
            print("Error: --app-path is required for --score-only", file=sys.stderr)
            sys.exit(1)
        score = score_visual_fidelity(bundle_dir, app_path)
        print(f"Visual fidelity score: {score}/100")
        return

    # Filter and copy assets from extracted dir if provided
    if extracted_dir:
        if not os.path.isdir(extracted_dir):
            print(f"Error: extracted dir not found: {extracted_dir}", file=sys.stderr)
            sys.exit(1)
        os.makedirs(assets_dir, exist_ok=True)
        kept = filter_and_copy_assets(catalog, icon_name, extracted_dir, assets_dir)
        print(f"Kept {kept} icon-related asset(s) in {assets_dir}")

        # Save pre-rendered Icon Image as scoring reference
        ref_src = find_prerendered_icon(catalog, icon_name, extracted_dir)
        if ref_src:
            shutil.copy2(ref_src, os.path.join(bundle_dir, "reference.png"))

    if not os.path.isdir(assets_dir):
        print(f"Error: Assets/ not found in {bundle_dir}", file=sys.stderr)
        sys.exit(1)

    layer_filenames, group_specs = resolve_layer_filenames(catalog, icon_name, assets_dir)

    # Reframe bitmap assets that need repositioning within the canvas
    reframed = reframe_assets(catalog, group_specs, assets_dir, layer_filenames)
    if reframed:
        print(f"Reframed {reframed} asset(s) for canvas positioning")

    # Flatten multi-layer SVG groups into single composite SVGs.
    # Icon Composer applies glass/material per-layer independently, which
    # washes out color differences between overlapping layers.  Merging
    # SVGs into one file matches the catalog compositor's "composite first,
    # then apply effects" behavior.
    merged = flatten_svg_groups(group_specs, assets_dir, layer_filenames)
    if merged:
        print(f"Flattened {merged} multi-layer SVG group(s)")

    doc = build_icon_composer_doc(catalog, icon_name, assets_dir, color_lookup, gradient_lookup, layer_filenames, group_specs)

    out_path = os.path.join(bundle_dir, "icon.json")
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)

    total_layers = sum(len(g.get("layers", [])) for g in doc.get("groups", []))
    print(f"Wrote Icon Composer icon.json to {out_path} ({len(doc['groups'])} groups, {total_layers} layers)")

    # Detect and report discrepancies
    discrepancies = collect_discrepancies(catalog, icon_name, assets_dir, doc, layer_filenames)
    discrepancies_json_path = os.path.join(bundle_dir, "discrepancies.json")

    if discrepancies:
        discrepancy_doc = {
            "app": os.path.basename(bundle_dir).replace(".icon", ""),
            "icon_name": icon_name,
            "clean": False,
            "discrepancies": discrepancies,
        }
        with open(discrepancies_json_path, "w") as f:
            json.dump(discrepancy_doc, f, indent=2)

        error_count = len(discrepancies)
        print(f"Found {error_count} discrepancy(ies) — see discrepancies.json")
    else:
        if os.path.isfile(discrepancies_json_path):
            os.remove(discrepancies_json_path)

    # Clean up stale errors.txt from previous runs
    errors_txt_path = os.path.join(bundle_dir, "errors.txt")
    if os.path.isfile(errors_txt_path):
        os.remove(errors_txt_path)

    # Visual fidelity scoring (if app path provided)
    if app_path:
        score = score_visual_fidelity(bundle_dir, app_path)
        print(f"Visual fidelity score: {score}/100")


if __name__ == "__main__":
    main()
