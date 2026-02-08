#!/usr/bin/env bash
#
# Extract icon data from an app's Assets.car into a .icon bundle
# (Icon Composer-style package with icon.json + Assets/).
#
# Requires: Asset Catalog Tinkerer (for asset extraction)
#   brew install asset-catalog-tinkerer
#   Or: https://github.com/insidegui/AssetCatalogTinkerer (v2.9+ for SVG)
#
# Usage:
#   ./recompose.sh /path/to/App.app
#
# Output is always <App Name>.icon in the current directory.
#
# Examples:
#   ./recompose.sh /System/Applications/Podcasts.app   → Podcasts.icon/
#   ./recompose.sh "/System/Applications/App Store.app" → App Store.icon/
#

set -e

# --- Require macOS ---------------------------------------------------------

if [[ "$(uname)" != "Darwin" ]]; then
  echo "error: This script requires macOS (uses assetutil and defaults)." >&2
  exit 1
fi

# --- Validate arguments ---------------------------------------------------

if [[ $# -ne 1 ]]; then
  echo "usage: $(basename "$0") /path/to/App.app" >&2
  exit 1
fi

APP_PATH="$1"

if [[ ! -d "$APP_PATH" || "$APP_PATH" != *.app ]]; then
  echo "error: Expected a .app bundle, got: $APP_PATH" >&2
  exit 1
fi

PLIST="$APP_PATH/Contents/Info.plist"
if [[ ! -f "$PLIST" ]]; then
  echo "error: Not an app bundle (no Contents/Info.plist): $APP_PATH" >&2
  exit 1
fi

CAR_PATH="$APP_PATH/Contents/Resources/Assets.car"
if [[ ! -f "$CAR_PATH" ]]; then
  echo "error: Assets.car not found at: $CAR_PATH" >&2
  exit 1
fi

ICON_NAME=$(defaults read "$APP_PATH/Contents/Info" CFBundleIconName 2>/dev/null || true)
if [[ -z "$ICON_NAME" ]]; then
  echo "error: CFBundleIconName not found in Info.plist: $PLIST" >&2
  exit 1
fi

# --- Require Asset Catalog Tinkerer (act) ----------------------------------

ACT_CMD=""
if command -v act &>/dev/null; then
  ACT_CMD="act"
elif [[ -x "/Applications/Asset Catalog Tinkerer.app/Contents/MacOS/act" ]]; then
  ACT_CMD="/Applications/Asset Catalog Tinkerer.app/Contents/MacOS/act"
else
  echo "error: Asset Catalog Tinkerer is required but not found." >&2
  echo "       brew install asset-catalog-tinkerer" >&2
  echo "       Or: https://github.com/insidegui/AssetCatalogTinkerer (v2.9+ for SVG)" >&2
  exit 1
fi

# --- Derive output path from app name -------------------------------------

APP_BASENAME="$(basename "$APP_PATH" .app)"
ICON_BUNDLE="./${APP_BASENAME}.icon"
CATALOG_PATH="$ICON_BUNDLE/catalog.json"

mkdir -p "$ICON_BUNDLE"
mkdir -p "$ICON_BUNDLE/Assets"

echo "App:        $APP_PATH"
echo "Assets.car: $CAR_PATH"
echo "Icon name:  $ICON_NAME"
echo "Output:     $ICON_BUNDLE"

# Dump full catalog JSON and filter to catalog metadata + icon-related assets
python3 - "$CAR_PATH" "$ICON_NAME" "$CATALOG_PATH" << 'PY'
import json
import sys
import subprocess

car_path = sys.argv[1]
icon_name = sys.argv[2]
output_path = sys.argv[3]

result = subprocess.run(
    ["assetutil", "-I", car_path],
    capture_output=True,
    text=True,
    check=True,
)
data = json.loads(result.stdout)

if not data:
    sys.exit("No data from assetutil")

metadata = data[0]
prefix = icon_name + "/"
filtered = [metadata]

for entry in data[1:]:
    if not isinstance(entry, dict):
        continue
    name = entry.get("Name") or ""
    if name == icon_name or name.startswith(prefix):
        filtered.append(entry)

with open(output_path, "w") as f:
    json.dump(filtered, f, indent=2)

print(f"Wrote {len(filtered) - 1} icon-related entries (+ catalog metadata) to {output_path}")
PY

# --- Extract assets with act -----------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONVERTER="$SCRIPT_DIR/recompose.py"

if [[ ! -f "$CONVERTER" ]]; then
  echo "error: converter not found: $CONVERTER" >&2
  exit 1
fi

# --- Compile helper tools if needed ----------------------------------------

THUMBNAIL_BIN="$SCRIPT_DIR/thumbnail"
THUMBNAIL_SRC="$SCRIPT_DIR/thumbnail.swift"

if [[ -f "$THUMBNAIL_SRC" ]]; then
  if [[ ! -x "$THUMBNAIL_BIN" ]] || [[ "$THUMBNAIL_SRC" -nt "$THUMBNAIL_BIN" ]]; then
    echo "Compiling thumbnail tool ..."
    swiftc "$THUMBNAIL_SRC" \
      -framework QuickLookThumbnailing -framework AppKit \
      -o "$THUMBNAIL_BIN"
  fi
fi

REFRAME_BIN="$SCRIPT_DIR/reframe"
REFRAME_SRC="$SCRIPT_DIR/reframe.swift"

if [[ -f "$REFRAME_SRC" ]]; then
  if [[ ! -x "$REFRAME_BIN" ]] || [[ "$REFRAME_SRC" -nt "$REFRAME_BIN" ]]; then
    echo "Compiling reframe tool ..."
    swiftc "$REFRAME_SRC" \
      -framework AppKit \
      -o "$REFRAME_BIN"
  fi
fi

TMP_EXTRACT=$(mktemp -d)
trap 'rm -rf "$TMP_EXTRACT"' EXIT

echo "Extracting assets with act ..."
if ! "$ACT_CMD" extract -i "$CAR_PATH" -o "$TMP_EXTRACT"; then
  echo "error: act failed to extract assets from: $CAR_PATH" >&2
  exit 1
fi

# --- Convert to Icon Composer format ---------------------------------------

python3 "$CONVERTER" \
  --icon-name "$ICON_NAME" \
  --extracted-dir "$TMP_EXTRACT" \
  --app-path "$APP_PATH" \
  "$ICON_BUNDLE"

echo "Created .icon bundle: $ICON_BUNDLE"
