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
4. Scores the output's visual fidelity against the original app icon (0-100)

## Requirements

- **macOS** (uses `assetutil`, `defaults`, `sips`)
- **Python 3.10+** (no third-party packages)
- **Xcode Command Line Tools** (for `swiftc` — compiles the thumbnail helper)
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
thumbnail.swift         # QuickLook thumbnail generator (compiled on first run)
lib/
  catalog.py            # Catalog parsing: colors, gradients, groups, layers
  composer.py           # Icon Composer document builder
  assets.py             # Asset filtering, deduplication, filename simplification
  scoring.py            # Visual fidelity scoring via perceptual hashing
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
   - **Build** the Icon Composer document from catalog data (`lib/composer.py`)
   - **Detect** discrepancies between catalog and output (`lib/discrepancies.py`)
   - **Score** visual fidelity against the original icon (`lib/scoring.py`)

## Fidelity scoring

The score (0-100) is a visual comparison using perceptual hashing (dHash):

1. Renders the generated `.icon` bundle via QuickLook
2. Renders the original `.app` icon via QuickLook
3. Resizes both thumbnails to 32x32 via `sips`
4. Computes a difference hash (dHash) for each — 992 bits capturing shape and gradient structure
5. Measures Hamming distance between the two hashes
6. Score = `100 * (1 - distance / total_bits)`

This captures how visually similar the reconstructed icon looks to the original, independent of the internal JSON representation.

## Discrepancy reporting

When the conversion can't fully represent the original asset catalog, a `discrepancies.json` file is included in the `.icon` bundle with structured entries. Each entry has a human-readable `description` field. Discrepancy types:

- **`bitmap_appearance_variant`**: Icon Composer uses a single `image-name` per layer, but some catalog icons use entirely different bitmap files for dark/light/tinted appearances. Only the default (light) variant is used.
- **`orphaned_asset`**: Files in `Assets/` not referenced by any layer in `icon.json` — typically the non-default appearance variants mentioned above.
- **`unmatched_catalog_layer`**: Layers present in the catalog that couldn't be matched to an extracted asset file.

If no discrepancies are found, the file is not created.

## Limitations

- `LayerShadowStyle` mapping is partially reverse-engineered (style 3 = "neutral" confirmed; other values assumed)
- Icon Composer may strip certain properties on re-save (e.g. default-value orientations, root `fill-specializations`)
- Only tested with macOS system app icons; third-party apps may use different catalog structures
