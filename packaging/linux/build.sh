#!/bin/bash
# Build Keepfold for Ubuntu (DESIGN.md 4.2 and 5.2) into dist/: keepfold_<v>_amd64.deb and the
# update tarball keepfold-<v>-linux-x86_64.tar.gz (one folder, <v>/, as in /opt/keepfold), each
# with its .sha256. Run it from a checkout on ubuntu-24.04, whose build runs on 26.04 too, with
# uv on PATH and the headers PyGObject and pycairo compile against:
#   sudo apt-get install -y libgirepository-2.0-dev libcairo2-dev libffi-dev pkg-config
set -euo pipefail
# uv's own Python (as the spike froze): its libpython and stdlib modules live outside /usr, so the
# spec's system-library filter keeps them; Ubuntu's /usr Python would lose libpython3.14.so
export UV_PYTHON_PREFERENCE=only-managed
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE=$ROOT/packaging/linux
WORK=$ROOT/build/linux
PKG=$WORK/deb
cd "$ROOT"
V=$(uv version --short)
rm -rf "$WORK"
mkdir -p "$WORK" dist

# 1. PyGObject and pycairo wheels, compiled against this Ubuntu's headers (the spike's versions)
uvx --python 3.14 pip wheel --no-deps PyGObject==3.58.1 pycairo==1.29.2 -w "$WORK/wheels"

# 2. Keepfold with the app group (PyInstaller, pywebview, pystray), then the two wheels. From
#    here on `uv run --no-sync`, so no sync removes them again.
uv sync --locked --no-dev --group app
uv pip install "$WORK"/wheels/*.whl

# 3. Freeze: $WORK/dist/keepfold/ holds keepfold-app, keepfold and _internal/
uv run --no-sync pyinstaller --noconfirm --clean --distpath "$WORK/dist" \
    --workpath "$WORK/pyinstaller" "$HERE/keepfold.spec"

# 4. The .deb's tree
mkdir -p "$PKG/opt/keepfold"
cp -a "$WORK/dist/keepfold" "$PKG/opt/keepfold/$V"
install -Dm755 "$HERE/launcher.sh" "$PKG/usr/bin/keepfold"
install -Dm755 "$HERE/launcher.sh" "$PKG/usr/bin/keepfold-app"
install -Dm644 -t "$PKG/usr/share/applications" "$HERE/io.github.alenissacsam_2.keepfold.desktop"
install -Dm644 -t "$PKG/usr/share/polkit-1/actions" "$HERE/io.github.alenissacsam2.keepfold.policy"
uv run --no-sync python - "$PKG/usr/share/icons/hicolor" <<'EOF'
# a hicolor PNG of each size scripts/make_icon.py draws for keepfold.ico
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from make_icon import SIZES, mark

for s in SIZES:
    out = Path(sys.argv[1], f"{s}x{s}", "apps", "keepfold.png")
    out.parent.mkdir(parents=True)
    mark(s).save(out)
EOF
chmod -R go-w "$PKG"
size=$(du -sk "$PKG" | cut -f1)
install -Dm755 -t "$PKG/DEBIAN" "$HERE/DEBIAN/prerm"
sed -e "s/@VERSION@/$V/" -e "s/@SIZE@/$size/" "$HERE/DEBIAN/control" > "$PKG/DEBIAN/control"

# 5. The .deb, the tarball and their checksums (sha256sum's form, which the updater reads)
dpkg-deb --build --root-owner-group "$PKG" "dist/keepfold_${V}_amd64.deb"
tar -C "$PKG/opt/keepfold" -czf "dist/keepfold-$V-linux-x86_64.tar.gz" "$V"
cd dist
for f in "keepfold_${V}_amd64.deb" "keepfold-$V-linux-x86_64.tar.gz"; do
    sha256sum "$f" > "$f.sha256"
done
ls -l "keepfold_${V}_amd64.deb"* "keepfold-$V-linux-x86_64.tar.gz"*
