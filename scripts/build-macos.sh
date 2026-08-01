#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Case PDF Downloader"
APP_PATH="$PROJECT_DIR/dist/$APP_NAME.app"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) ARCH_LABEL="Apple-Silicon" ;;
  x86_64) ARCH_LABEL="Intel" ;;
  *) ARCH_LABEL="$ARCH" ;;
esac
DMG_PATH="$PROJECT_DIR/dist/$APP_NAME-macOS-$ARCH_LABEL.dmg"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
export PLAYWRIGHT_BROWSERS_PATH=0
.venv/bin/python -m playwright install chromium
.venv/bin/pyinstaller --noconfirm --clean CasePDFDownloader.spec

if [[ ! -d "$APP_PATH" ]]; then
  echo "Expected application bundle was not created: $APP_PATH" >&2
  exit 1
fi

if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  echo "Signing $APP_NAME.app..."
  codesign \
    --force \
    --options runtime \
    --timestamp \
    --sign "$APPLE_SIGNING_IDENTITY" \
    "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "APPLE_SIGNING_IDENTITY is not set; creating an unsigned development package."
fi

DMG_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/case-pdf-downloader.XXXXXX")"
cleanup() {
  rm -rf "$DMG_STAGE"
}
trap cleanup EXIT

ditto "$APP_PATH" "$DMG_STAGE/$APP_NAME.app"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$DMG_PATH"
fi

if [[ -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "${APPLE_SIGNING_IDENTITY:-}" ]]; then
    echo "APPLE_NOTARY_PROFILE requires APPLE_SIGNING_IDENTITY." >&2
    exit 1
  fi
  echo "Submitting the DMG to Apple for notarization..."
  xcrun notarytool submit \
    "$DMG_PATH" \
    --keychain-profile "$APPLE_NOTARY_PROFILE" \
    --wait
  xcrun stapler staple "$DMG_PATH"
fi

echo
echo "macOS package created:"
echo "$DMG_PATH"
