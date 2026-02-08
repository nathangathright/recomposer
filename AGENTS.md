# Recomposer — Agent Guide

Reverse-engineers macOS app icons from compiled asset catalogs (`Assets.car`) into editable Icon Composer `.icon` bundles.

## Quick start

```sh
# Single app
./recompose.sh /System/Applications/Podcasts.app     # -> Podcasts.icon/

# All first-party apps (scores every icon and prints average)
./recompose_all.sh

# Re-score an existing bundle without reconverting
python3 recompose.py --score-only --icon-name AppIcon \
  --app-path /System/Applications/Podcasts.app Podcasts.icon
```

## Verification

After any change to the pipeline scripts or Python modules, run the smoke test. It covers all emitted discrepancy types and structural edge cases. Watch for new failures, score regressions, and stderr warnings.

| App | Score | Coverage |
|-----|-------|----------|
| Script Editor | 85 | Clean composable (happy-path baseline, 4 groups / 4 layers) |
| Preview | 74 | `bitmap_appearance_variant` + `orphaned_asset` (8 discrepancies, dark + tinted) |
| Games | 68 | `orphaned_asset` + `unmatched_catalog_layer` (10 discrepancies, 21 layers, most complex, 3 groups flattened) |
| Dictionary | 64 | `orphaned_asset` + `locale_variant_unused` (2 discrepancies, 9 locale glyph variants, 2 groups / 2 layers) |
| Boot Camp Assistant | 34 | `legacy_bitmap_fallback` (1 discrepancy, single-layer bitmap, non-composable path) |

```sh
mkdir -p output
cd output
for app in "Script Editor" Preview Games Dictionary "Boot Camp Assistant"; do
  ../recompose.sh "/System/Applications/$app.app" 2>/dev/null \
    || ../recompose.sh "/Applications/$app.app" 2>/dev/null \
    || ../recompose.sh "/System/Applications/Utilities/$app.app" 2>/dev/null
done
```

Note: Script Editor and Boot Camp Assistant live under `/System/Applications/Utilities/`,
not `/System/Applications/`. The fallback chain above handles this automatically.
`act` is not on `$PATH` by default — the script finds it at
`/Applications/Asset Catalog Tinkerer.app/Contents/MacOS/act`.

## Pipeline stages

Each run of `recompose.sh` executes these stages in order:

```
1. EXTRACT METADATA   assetutil -I Assets.car -> catalog.json
2. EXTRACT ASSETS     act extract -> temp dir of SVG/PNG/PDF files
3. FILTER & COPY      lib/assets.py: copy icon-related files into Assets/
4. RESOLVE & BUILD    lib/assets.py + lib/composer.py: match layers to files,
                      rename to clean names, reframe inset bitmaps,
                      flatten multi-layer SVG groups, generate icon.json
5. DETECT ISSUES      lib/discrepancies.py: compare catalog vs icon.json
6. SCORE              lib/scoring.py: visual comparison via perceptual hash
```

Data is parsed once and threaded forward: `main()` computes lookups
(`color_lookup`, `gradient_lookup`) and passes them to each stage.
Each `LayerSpec` carries its own `rendition_stem` (the RenditionName
without extension), so layer-to-file matching is self-contained.
`resolve_layer_filenames()` returns both the filename mapping and
the parsed `group_specs`, which are forwarded to
`build_icon_composer_doc()` and `collect_discrepancies()` so neither
re-parses the catalog or re-matches layers.

## Module responsibilities

```
recompose.sh          Shell orchestrator: validates .app, dumps catalog,
                      runs act, invokes recompose.py
recompose.py          CLI entry point: argument parsing + main() that
                      wires the pipeline stages together
recompose_all.sh      Batch runner for all first-party macOS apps
thumbnail.swift       QuickLook thumbnail generator (compiled on first run)
reframe.swift         Bitmap repositioning within canvas (compiled on first run)

lib/
  catalog.py          Catalog parsing. Owns:
                        - Color/gradient lookup builders (incl. sRGB → Display P3 conversion)
                        - LayerSpec / GroupSpec data classes (LayerSpec.rendition_stem
                          carries the RenditionName stem for asset matching)
                        - collect_groups_from_catalog() — the core catalog walker
                          (incl. group-level opacity from IconImageStack + layer geometry)
                        - get_canvas_size() — canvas dimensions from IconImageStack
                        - Appearance constants (APPEARANCE_MAP, LIGHT_APPEARANCES)

  composer.py         Icon Composer document builder. Owns:
                        - get_fill_from_catalog() — derives background fill
                        - resolve_fill_ref_to_layer_fill() — per-layer fill resolution
                        - build_icon_composer_doc() — assembles the final icon.json dict

  assets.py           Asset file operations. Owns:
                        - find_asset_file_for_layer() — matches catalog names to files
                        - filter_and_copy_assets() — copies + deduplicates from act output
                        - resolve_layer_filenames() — matches + renames in one pass, returns mapping
                        - flatten_svg_groups() — merges multi-layer SVG groups into single
                          composite SVGs (Icon Composer applies glass per-layer independently;
                          the catalog compositor composites first, so we pre-merge to match)
                        - reframe_assets() — repositions inset bitmaps within the canvas

  scoring.py          Visual fidelity scoring. Owns:
                        - score_visual_fidelity() — QuickLook + dHash comparison (0-100)
                        - BMP pixel reader, dHash, Hamming distance

  discrepancies.py    Discrepancy detection and reporting. Owns:
                        - collect_discrepancies() — returns structured dicts
```

## Where to make changes

| Goal                              | Start here                |
|-----------------------------------|---------------------------|
| Fix asset matching / missing files | `lib/assets.py`          |
| Fix layer merging / glass rendering | `lib/assets.py` (`flatten_svg_groups`) |
| Handle new catalog properties      | `lib/catalog.py`         |
| Fix icon.json structure            | `lib/composer.py`        |
| Improve visual scoring             | `lib/scoring.py`         |
| Add a new discrepancy type         | `lib/discrepancies.py`   |
| Change CLI behavior                | `recompose.py`           |
| Change extraction/orchestration    | `recompose.sh`           |

## Discrepancy types

Each `.icon` bundle may contain a `discrepancies.json` file with structured entries. Each entry has a human-readable `description` field. The `type` field in each entry is one of:

### `bitmap_appearance_variant`

Icon Composer uses a single `image-name` per layer. When the catalog uses entirely different bitmap files for dark/light/tinted appearances, only the default (light) variant is used. The others remain in `Assets/` but aren't referenced.

**Fields:** `type`, `group`, `layer`, `appearance`, `description`

### `orphaned_asset`

A file exists in `Assets/` but is not referenced by any layer in `icon.json`. Usually the non-default appearance variants from above, or extra extraction artifacts.

**Fields:** `type`, `asset_file`, `description`

### `unmatched_catalog_layer`

A layer is present in the catalog (Vector/Image entry) but couldn't be matched to any extracted asset file. Typically happens when multiple catalog layers share the same RenditionName and only one gets the file.

**Fields:** `type`, `layer`, `description`

### `legacy_bitmap_fallback`

The icon contains only pre-rendered "Icon Image" bitmaps with no composable layers. The highest-resolution bitmap is used as a single-layer fallback.

**Fields:** `type`, `description`

### `locale_variant_unused`

A locale-specific glyph variant (e.g. Japanese, Arabic) exists but was not selected as the default. The Latin variant is preferred.

**Fields:** `type`, `description`

## Known limitations (inherent to Icon Composer format)

These cannot be fixed by improving the code — they are fundamental constraints:

- **One image per layer**: Icon Composer does not support per-appearance image switching. Bitmap dark/tinted variants are lost.
- **No locale support**: Icon Composer has no mechanism for locale-specific layer variants. Only one glyph can be selected (we pick Latin).
- **Per-layer glass rendering**: Icon Composer applies glass/material effects independently per layer. The catalog compositor composites layers within a group first, then applies group-level effects. We work around this by merging multi-layer SVG groups into single composite SVGs (`flatten_svg_groups`). Groups mixing SVGs and PNGs cannot be merged this way.
- **Shadow style mapping**: `LayerShadowStyle` values 2 ("layer-color"/Chromatic) and 3 ("neutral") are confirmed. Values 0 and 1 are not observed in practice. When shadow kind is `"none"`, the opacity value is ignored by Icon Composer.
- **Legacy bitmap icons**: Pre-rendered icons (e.g. Boot Camp Assistant) produce a flat single-layer result with no composable structure.

## Requirements

- macOS (uses `assetutil`, `defaults`, `sips`, `swiftc`)
- Python 3.10+ (no third-party packages)
- Xcode Command Line Tools
- [Asset Catalog Tinkerer](https://github.com/insidegui/AssetCatalogTinkerer) v2.9+ (`brew install asset-catalog-tinkerer`)
