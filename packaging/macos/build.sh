#!/usr/bin/env bash
# Build Keepfold.app and its release files on an Apple Silicon Mac: `bash packaging/macos/build.sh`.
# Into dist/, each with its .sha256 (shasum's "<hex>  <name>" form, as KeepfoldSetup.exe.sha256):
#   Keepfold-<v>-macos-arm64.dmg  for people: the app plus an Applications link to drag it onto;
#   Keepfold-<v>-macos-arm64.zip  for Update now (keepfold.update), the same .app.
# The app itself stays in dist/macos-build/Keepfold.app. Needs Xcode's command-line tools and a
# python.org Python 3.14 on PATH (CI: actions/setup-python), since uv's own Pythons are a known
# PyInstaller trouble spot (DESIGN §4.1).
set -euo pipefail
cd "$(dirname "$0")/../.."
export UV_PYTHON_PREFERENCE=only-system

STAGE=dist/macos-build  # keepfold.spec reads the icon from here
APP=$STAGE/Keepfold.app
rm -rf "$STAGE"
mkdir -p "$STAGE"

uv sync --locked --no-dev --group app
VERSION=$(uv run --no-sync python -c 'from importlib.metadata import version; print(version("keepfold"))')
NAME=Keepfold-$VERSION-macos-$(uname -m)

# the icon, drawn by the code that draws the Windows .ico (keepfold.app.desktop.mark)
uv run --no-sync python scripts/make_icon.py --iconset "$STAGE/Keepfold.iconset"
iconutil -c icns -o "$STAGE/Keepfold.icns" "$STAGE/Keepfold.iconset"

uv run --no-sync pyinstaller --noconfirm --clean --distpath "$STAGE" --workpath "$STAGE/work" \
  packaging/macos/keepfold.spec

codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

mkdir "$STAGE/dmg"
ditto "$APP" "$STAGE/dmg/Keepfold.app"
ln -s /Applications "$STAGE/dmg/Applications"
# GitHub's Mac runners sometimes answer "Resource busy" to a first hdiutil create
for try in 1 2 3; do
  hdiutil create -volname Keepfold -srcfolder "$STAGE/dmg" -format UDZO -ov "dist/$NAME.dmg" &&
    break
  [ "$try" = 3 ] && exit 1
  sleep 10
done

ditto -c -k --keepParent "$APP" "dist/$NAME.zip"

cd dist
for f in "$NAME.dmg" "$NAME.zip"; do
  shasum -a 256 "$f" > "$f.sha256"
done
du -sh "../$APP" "$NAME.dmg" "$NAME.zip"
