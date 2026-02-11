#!/usr/bin/env bash
set -euo pipefail

# ========================================
# Project Kestrel macOS Installer Packager
# (Creates .pkg from already-built apps)
# ========================================

echo
printf "%s\n" "========================================"
printf "%s\n" "Project Kestrel macOS PKG Packager"
printf "%s\n" "========================================"
echo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
RELEASE_ROOT="${PROJECT_ROOT}/release"

cd "${PROJECT_ROOT}"

# Find the most recent release directory
RELEASE_DIR=$(ls -td "${RELEASE_ROOT}"/alpha-* 2>/dev/null | head -n 1)

if [[ -z "${RELEASE_DIR}" || ! -d "${RELEASE_DIR}" ]]; then
  echo "[ERROR] No release directory found in ${RELEASE_ROOT}"
  exit 1
fi

APP_VERSION=$(basename "${RELEASE_DIR}")
echo "Packaging release: ${APP_VERSION}"

# Verify apps exist
if [[ ! -d "${RELEASE_DIR}/kestrel_analyzer.app" ]]; then
  echo "[ERROR] kestrel_analyzer.app not found in ${RELEASE_DIR}"
  exit 1
fi

if [[ ! -d "${RELEASE_DIR}/visualizer.app" ]]; then
  echo "[ERROR] visualizer.app not found in ${RELEASE_DIR}"
  exit 1
fi

echo
printf "%s\n" "========================================"
printf "%s\n" "Building macOS installer (.pkg)"
printf "%s\n" "========================================"
echo

PKG_ROOT="${RELEASE_DIR}/pkgroot"
APP_INSTALL_DIR="${PKG_ROOT}/Applications/Project Kestrel"
PKG_OUTPUT="${RELEASE_DIR}/ProjectKestrel-${APP_VERSION}.pkg"
PKG_SCRIPTS="${RELEASE_DIR}/pkg-scripts"

rm -rf "${PKG_ROOT}"
rm -rf "${PKG_SCRIPTS}"
mkdir -p "${APP_INSTALL_DIR}"
mkdir -p "${PKG_SCRIPTS}"
cp -R "${RELEASE_DIR}/kestrel_analyzer.app" "${APP_INSTALL_DIR}/"
cp -R "${RELEASE_DIR}/visualizer.app" "${APP_INSTALL_DIR}/"

cat > "${PKG_SCRIPTS}/postinstall" <<'EOS'
#!/bin/bash
set -euo pipefail

IM_URL="https://imagemagick.org/script/download.php"
if command -v magick >/dev/null 2>&1; then
  exit 0
fi

if /usr/bin/osascript <<EOF
display dialog "ImageMagick was not detected.\n\nProject Kestrel can use ImageMagick for RAW image support.\nWould you like to open the download page now?" buttons {"Cancel","Open Download"} default button "Open Download"
EOF
then
  /usr/bin/open "${IM_URL}" || true
fi
EOS
chmod +x "${PKG_SCRIPTS}/postinstall"

pkgbuild \
  --root "${PKG_ROOT}" \
  --scripts "${PKG_SCRIPTS}" \
  --identifier "org.ProjectKestrel" \
  --version "${APP_VERSION}" \
  --install-location "/" \
  "${PKG_OUTPUT}"

echo
printf "%s\n" "========================================"
printf "%s\n" "PKG build completed"
printf "%s\n" "========================================"
echo
printf "Installer: %s\n" "${PKG_OUTPUT}"
