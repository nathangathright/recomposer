# Recomposer

Reverse-engineer a macOS app's compiled asset catalog (`Assets.car`) into an [Icon Composer](https://developer.apple.com/icon-composer/) `.icon` file that can be opened, inspected, and edited.

## What it does

1. Extracts icon metadata from `Assets.car` using `assetutil`
2. Extracts vector/image assets using [Asset Catalog Tinkerer](https://github.com/insidegui/AssetCatalogTinkerer)
3. Converts the catalog metadata into Icon Composer's JSON format, including:
   - Root fill (background gradient) with tinted appearance specialization
   - Per-group shadow, translucency, specular, and blur properties
   - Per-layer fill and opacity with dark/tinted appearance specializations
   - Glass effects
   - sRGB to Display P3 color space conversion
4. Reframes bitmap layers to their correct canvas position when needed
5. Detects and reports conversion discrepancies
6. Scores the output's visual fidelity against the original app icon (0–100)

## Requirements

- **macOS** (uses `assetutil`, `defaults`, `sips`)
- **Python 3.10+** (no third-party packages)
- **Xcode Command Line Tools** (for `swiftc` — compiles helper binaries)
- **[Asset Catalog Tinkerer](https://github.com/insidegui/AssetCatalogTinkerer)** (v2.9+) for asset extraction

```sh
brew install asset-catalog-tinkerer
```

## Usage

```sh
# Pass a .app bundle — output is always <App Name>.icon in the current directory
./recompose.sh /System/Applications/Podcasts.app       # -> Podcasts.icon/
./recompose.sh "/System/Applications/App Store.app"     # -> App Store.icon/

# Process all first-party apps and compute average fidelity score
./recompose_all.sh                                       # -> output/*.icon/

# Re-score an existing bundle without reconverting
python3 recompose.py --score-only \
  --icon-name AppIcon --app-path /System/Applications/Podcasts.app Podcasts.icon
```

## Output

The `.icon` bundle is a directory that Icon Composer can open directly:

```
Podcasts.icon/
  icon.json             # Icon Composer document
  catalog.json          # Raw assetutil catalog (kept for debugging)
  discrepancies.json    # Structured discrepancy data (only when issues exist)
  reference.png         # Pre-rendered icon from Assets.car (used for scoring)
  Assets/
    1_person.svg        # Simplified layer filenames
    2_circle2.svg
    3_circle1.svg
```

## Project structure

```
recompose.sh            # Shell orchestrator (validates app, dumps catalog, runs act)
recompose.py            # CLI entry point (argument parsing + pipeline wiring)
recompose_all.sh        # Batch runner for all first-party macOS apps (output/)
batch_score.sh          # Re-score all existing .icon bundles without reconverting
thumbnail.swift         # QuickLook thumbnail generator (compiled on first run)
reframe.swift           # Bitmap reframing tool (positions layers within a canvas)
lib/
  catalog.py            # Catalog parsing: colors, gradients, groups, layers, sRGB→P3
  composer.py           # Icon Composer document builder (icon.json)
  assets.py             # Asset filtering, deduplication, reframing, filename resolution
  scoring.py            # Visual fidelity scoring via multi-metric pixel comparison
  discrepancies.py      # Discrepancy detection and structured reporting
```

See [AGENTS.md](AGENTS.md) for detailed module responsibilities and how to extend.

## How it works

The pipeline is orchestrated by `recompose.sh`:

1. Validates the `.app` bundle and reads `CFBundleIconName` from `Info.plist`
2. Runs `assetutil -I` to dump catalog metadata, filters to icon-related entries
3. Calls `act` (Asset Catalog Tinkerer CLI) to extract binary assets to a temp directory
4. Invokes `recompose.py` which runs the conversion pipeline:
   - **Filter & copy** assets from the extraction directory (`lib/assets.py`)
   - **Resolve & rename** layers to asset files in a single pass (`lib/assets.py`)
   - **Reframe** bitmap layers that need repositioning within their canvas (`reframe.swift`)
   - **Build** the Icon Composer document from catalog data (`lib/composer.py`)
   - **Detect** discrepancies between catalog and output (`lib/discrepancies.py`)
   - **Score** visual fidelity against the original icon (`lib/scoring.py`)

## Fidelity scoring

The score (0–100) is a weighted multi-metric comparison:

1. Uses the pre-rendered `reference.png` from `Assets.car` as ground truth (falls back to a QuickLook thumbnail of the `.app` bundle if unavailable)
2. Renders the generated `.icon` bundle via QuickLook
3. Crops both images to their content bounds (trimming transparent padding)
4. Resizes both to 256×256 via `sips`
5. Computes three metrics:
   - **Color RMSE** (50%) — root-mean-square error across all RGB pixels
   - **SSIM** (40%) — structural similarity over 8×8 non-overlapping windows
   - **Histogram intersection** (10%) — per-channel histogram overlap
6. Final score = weighted blend of the three metrics

## Discrepancy reporting

When the conversion can't fully represent the original asset catalog, a `discrepancies.json` file is included in the `.icon` bundle with structured entries. Each entry has a human-readable `description` field. Discrepancy types:

- **`bitmap_appearance_variant`**: Icon Composer uses a single `image-name` per layer, but some catalog icons use entirely different bitmap files for dark/light/tinted appearances. Only the default (light) variant is used.
- **`orphaned_asset`**: Files in `Assets/` not referenced by any layer in `icon.json` — typically the non-default appearance variants mentioned above.
- **`unmatched_catalog_layer`**: Layers present in the catalog that couldn't be matched to an extracted asset file.
- **`legacy_bitmap_fallback`**: The icon contains only pre-rendered "Icon Image" bitmaps with no composable layers. The highest-resolution bitmap is used as a single-layer fallback.
- **`locale_variant_unused`**: A locale-specific glyph variant (e.g. Japanese, Arabic) exists but was not selected. The Latin variant is preferred.

If no discrepancies are found, the file is not created.

## Shadow styles

Icon Composer supports three shadow kinds per group:

| Kind | JSON value | Description |
|------|------------|-------------|
| Neutral | `"neutral"` | Standard gray drop shadow |
| Chromatic | `"layer-color"` | Shadow colored by the layer content |
| None | `"none"` | No shadow (opacity value is ignored) |

The catalog's `LayerShadowStyle` integer maps to these kinds. Styles 2 (`"layer-color"`) and 3 (`"neutral"`) are confirmed; styles 0 and 1 are not observed in practice.

## Limitations

- **One image per layer**: Icon Composer does not support per-appearance image switching. Bitmap dark/tinted variants are lost.
- **No locale support**: Icon Composer has no mechanism for locale-specific layer variants. Only one glyph can be selected (Latin preferred).
- **Legacy bitmap icons**: Pre-rendered icons (e.g. Boot Camp Assistant) produce a flat single-layer result with no composable structure.
- **Rendering differences**: Icon Composer applies its own 3D lighting, shadows, and specular highlights that may differ subtly from the original `.app` rendering — this is an inherent ceiling on fidelity scores.
- Icon Composer may strip certain properties on re-save (e.g. default-value orientations, root `fill-specializations`)
- Only tested with macOS system app icons; third-party apps may use different catalog structures
