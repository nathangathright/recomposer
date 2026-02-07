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
4. Scores the output's fidelity against the original catalog (0-100)

## Requirements

- **macOS** (uses `assetutil`, `defaults`)
- **Python 3.10+** (no third-party packages)
- **[Asset Catalog Tinkerer](https://github.com/insidegui/AssetCatalogTinkerer)** (v2.9+) for asset extraction

```sh
brew install asset-catalog-tinkerer
```

## Usage

```sh
# Pass a .app bundle — output is always <App Name>.icon in the current directory
./extract_icon_json.sh /System/Applications/Podcasts.app       # → Podcasts.icon/
./extract_icon_json.sh "/System/Applications/App Store.app"    # → App Store.icon/

# Re-score an existing bundle without reconverting
python3 catalog_to_icon_composer.py --score-only Podcasts.icon
```

## Output

The `.icon` bundle is a directory that Icon Composer can open directly:

```
Podcasts.icon/
  icon.json       # Icon Composer document
  catalog.json    # Raw assetutil catalog (kept for reference/scoring)
  Assets/
    1_person.svg   # Simplified layer filenames
    2_circle2.svg
    3_circle1.svg
```

## How it works

The pipeline is two scripts:

**`extract_icon_json.sh`** — Orchestrates the process:
- Validates the `.app` bundle and reads `CFBundleIconName` from `Info.plist`
- Runs `assetutil -I` to dump catalog metadata, filters to icon-related entries
- Calls `act` (Asset Catalog Tinkerer CLI) to extract binary assets to a temp directory
- Invokes the converter with `--icon-name` and `--extracted-dir`

**`catalog_to_icon_composer.py`** — Converts catalog JSON to Icon Composer format:
- Filters and copies only icon-related assets from the extraction directory
- Identifies background gradients by finding Named Gradients not referenced by any layer
- Walks `IconImageStack` / `IconGroup` entries to build groups with correct layer ordering
- Maps catalog properties to Icon Composer keys (`LayerShadowStyle` -> shadow kind, `LayerBlurStrength` -> `blur-material`, `LayerHasSpecular` -> `specular`, etc.)
- Uses the Light appearance as default values; Dark/Tinted differences become `fill-specializations` and `opacity-specializations`
- Scores the generated document against the source catalog

## Fidelity scoring

The score (0-100) breaks down as:

| Category | Points | What it checks |
|----------|--------|----------------|
| Root fill | 20 | Gradient type, colors, orientation, tinted specialization |
| Groups/layers | 15 | Correct count of groups and layers |
| Layer fills | 30 | Fill presence + fill-specialization coverage per appearance |
| Glass | 10 | Specular/glass applied to correct layers |
| Color accuracy | 15 | Root fill color values match catalog |
| Opacity | 10 | Opacity specialization coverage |

## Limitations

- `LayerShadowStyle` mapping is partially reverse-engineered (style 3 = "neutral" confirmed; other values assumed)
- Icon Composer may strip certain properties on re-save (e.g. default-value orientations, root `fill-specializations`)
- Only tested with macOS system app icons; third-party apps may use different catalog structures
