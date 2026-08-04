#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IOS_DIR="$ROOT_DIR/ios"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/marketing/app-store/screenshots/en-US/6.9-inch}"
DERIVED_DATA="${DERIVED_DATA:-/tmp/kevin-app-store-screenshots-derived}"
DEVICE_NAME="${DEVICE_NAME:-Kevin App Store Screenshots}"
DEVICE_PROFILE="${DEVICE_PROFILE:-iPhone 17 Pro Max}"
SIMULATOR_RUNTIME="${SIMULATOR_RUNTIME:-com.apple.CoreSimulator.SimRuntime.iOS-26-0}"
BUNDLE_ID="com.kevin.callscreen"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required to select the simulator by name." >&2
  exit 1
fi

DEVICE_UDID="${DEVICE_UDID:-$(
  xcrun simctl list devices available --json \
    | jq -r --arg name "$DEVICE_NAME" '.devices[][] | select(.name == $name) | .udid' \
    | head -n 1
)}"

if [[ -z "$DEVICE_UDID" ]]; then
  DEVICE_UDID="$(xcrun simctl create "$DEVICE_NAME" "$DEVICE_PROFILE" "$SIMULATOR_RUNTIME")"
fi

mkdir -p "$OUTPUT_DIR"

(
  cd "$IOS_DIR"
  xcodegen generate
  xcodebuild \
    -project Kevin.xcodeproj \
    -scheme Kevin \
    -configuration Debug \
    -sdk iphonesimulator \
    -destination "id=$DEVICE_UDID" \
    -derivedDataPath "$DERIVED_DATA" \
    CODE_SIGNING_ALLOWED=NO \
    build
)

xcrun simctl boot "$DEVICE_UDID" >/dev/null 2>&1 || true
xcrun simctl bootstatus "$DEVICE_UDID" -b
xcrun simctl ui "$DEVICE_UDID" appearance light
xcrun simctl status_bar "$DEVICE_UDID" override \
  --time "9:41" \
  --batteryState charged \
  --batteryLevel 100 \
  --cellularBars 4 \
  --wifiBars 3
xcrun simctl install "$DEVICE_UDID" "$DERIVED_DATA/Build/Products/Debug-iphonesimulator/Kevin.app"

capture() {
  local scenario="$1"
  local filename="$2"

  xcrun simctl terminate "$DEVICE_UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
  SIMCTL_CHILD_APP_STORE_SCREENSHOT_SCENARIO="$scenario" \
    xcrun simctl launch --terminate-running-process "$DEVICE_UDID" "$BUNDLE_ID" >/dev/null
  sleep 2
  xcrun simctl io "$DEVICE_UDID" screenshot "$OUTPUT_DIR/$filename"
}

capture "business-live" "default-01-business-live.png"
capture "business-recents" "default-02-business-recents.png"
capture "business-detail" "default-03-business-detail.png"
capture "personal-live" "personal-01-live.png"
capture "personal-recents" "personal-02-recents.png"
capture "personal-detail" "personal-03-detail.png"

echo "Captured App Store screenshots in $OUTPUT_DIR"
sips -g pixelWidth -g pixelHeight "$OUTPUT_DIR"/*.png
