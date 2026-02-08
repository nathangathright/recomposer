#!/usr/bin/env bash
#
# Run recompose.sh against all first-party macOS apps,
# capture fidelity scores, and compute the average.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXTRACT="$SCRIPT_DIR/recompose.sh"
OUTPUT_DIR="$SCRIPT_DIR/output"

mkdir -p "$OUTPUT_DIR"

# Collect all first-party app paths
APPS=()

# System Applications (all first-party)
for app in /System/Applications/*.app; do
  [ -d "$app" ] && APPS+=("$app")
done

# System Utilities (all first-party)
for app in /System/Applications/Utilities/*.app; do
  [ -d "$app" ] && APPS+=("$app")
done

# First-party apps in /Applications
for name in "Safari.app" "Numbers.app" "Pages.app" "Keynote.app" \
            "GarageBand.app" "iMovie.app" "Xcode.app" "Apple Configurator 2.app" \
            "Developer.app" "TestFlight.app" "Reality Composer.app"; do
  if [ -d "/Applications/$name" ]; then
    APPS+=("/Applications/$name")
  fi
done

echo "Found ${#APPS[@]} first-party apps to process"
echo "=========================================="

SCORES=()
FAILED=()
SKIPPED=()
PROCESSED=0

for app in "${APPS[@]}"; do
  app_name="$(basename "$app" .app)"
  echo ""
  echo "--- Processing: $app_name ---"

  # Pre-check: does it have Assets.car and CFBundleIconName?
  car_path="$app/Contents/Resources/Assets.car"
  if [ ! -f "$car_path" ]; then
    echo "  SKIP: No Assets.car"
    SKIPPED+=("$app_name (no Assets.car)")
    continue
  fi

  icon_name=$(defaults read "$app/Contents/Info" CFBundleIconName 2>/dev/null || true)
  if [ -z "$icon_name" ]; then
    echo "  SKIP: No CFBundleIconName"
    SKIPPED+=("$app_name (no CFBundleIconName)")
    continue
  fi

  # Run the extraction script from the output directory, capture output
  output=$(cd "$OUTPUT_DIR" && "$EXTRACT" "$app" 2>&1) || {
    echo "  FAILED: extraction error"
    echo "$output" | tail -3
    FAILED+=("$app_name")
    continue
  }

  # Parse score from output
  score_line=$(echo "$output" | grep "Visual fidelity score:" || true)
  if [ -z "$score_line" ]; then
    echo "  FAILED: no score in output"
    FAILED+=("$app_name (no score)")
    continue
  fi

  score=$(echo "$score_line" | sed 's/.*score: \([0-9]*\).*/\1/')
  echo "  Score: $score/100"

  SCORES+=("$score")
  PROCESSED=$((PROCESSED + 1))
done

echo ""
echo "=========================================="
echo "RESULTS SUMMARY"
echo "=========================================="
echo ""
echo "Processed: $PROCESSED apps"
echo "Skipped:   ${#SKIPPED[@]} apps"
echo "Failed:    ${#FAILED[@]} apps"
echo ""

if [ ${#SKIPPED[@]} -gt 0 ]; then
  echo "Skipped apps:"
  for s in "${SKIPPED[@]}"; do
    echo "  - $s"
  done
  echo ""
fi

if [ ${#FAILED[@]} -gt 0 ]; then
  echo "Failed apps:"
  for f in "${FAILED[@]}"; do
    echo "  - $f"
  done
  echo ""
fi

if [ ${#SCORES[@]} -gt 0 ]; then
  echo "Individual scores:"
  # Re-run through processed apps to pair names with scores
  idx=0
  for app in "${APPS[@]}"; do
    app_name="$(basename "$app" .app)"
    car_path="$app/Contents/Resources/Assets.car"
    [ ! -f "$car_path" ] && continue
    icon_name=$(defaults read "$app/Contents/Info" CFBundleIconName 2>/dev/null || true)
    [ -z "$icon_name" ] && continue
    # Check if this app has a .icon bundle (i.e. it was processed successfully)
    if [ -d "$OUTPUT_DIR/${app_name}.icon" ] && [ $idx -lt ${#SCORES[@]} ]; then
      printf "  %-30s %s/100\n" "$app_name" "${SCORES[$idx]}"
      idx=$((idx + 1))
    fi
  done

  # Compute average
  total=0
  for s in "${SCORES[@]}"; do
    total=$((total + s))
  done
  avg=$(echo "scale=1; $total / ${#SCORES[@]}" | bc)
  echo ""
  echo "=========================================="
  printf "AVERAGE SCORE: %s/100 (across %d apps)\n" "$avg" "${#SCORES[@]}"
  echo "=========================================="
fi
