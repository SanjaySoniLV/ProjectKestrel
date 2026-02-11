#!/usr/bin/env bash
set -euo pipefail

# ========================================
# Project Kestrel macOS App Builder
# (PyInstaller build only, no .pkg)
# ========================================

echo
printf "%s\n" "========================================"
printf "%s\n" "Project Kestrel macOS App Builder"
printf "%s\n" "========================================"
echo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
RELEASE_ROOT="${PROJECT_ROOT}/release"

RELEASE_TS="$(date "+%Y.%m.%d.%H.%M")"
RELEASE_NAME="Project Kestrel a${RELEASE_TS}"
APP_VERSION="alpha-${RELEASE_TS}"

printf "Using release name: %s\n" "${RELEASE_NAME}"
printf "Using app version: %s\n" "${APP_VERSION}"

cd "${PROJECT_ROOT}"

# Create VERSION.txt and copy into analyzer/visualizer
VERSION_FILE="VERSION.txt"
{
  echo "Build: ${RELEASE_TS}"
  echo "Version: ${APP_VERSION}"
} > "${VERSION_FILE}"
cp -f "${VERSION_FILE}" "analyzer/VERSION.txt"
cp -f "${VERSION_FILE}" "visualizer/VERSION.txt"

# Create release directory based on version
RELEASE_DIR="${RELEASE_ROOT}/${APP_VERSION}"
mkdir -p "${RELEASE_DIR}"

echo "Checking prerequisites..."
echo

ensure_venv() {
  if [[ -n "${VIRTUAL_ENV:-}" && "${VIRTUAL_ENV}" == *"/.venv2" ]]; then
    return 0
  fi

  if [[ -f "${PROJECT_ROOT}/.venv2/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "${PROJECT_ROOT}/.venv2/bin/activate"
  else
    echo "[WARNING] .venv2 not found at ${PROJECT_ROOT}/.venv2"
    echo "[WARNING] Create it and install requirements-macos.txt before running."
  fi
}

build_component() {
  local component_name="$1"
  local component_artifact="$2"
  local component_spec="$3"
  local component_build_label="$4"

  ensure_venv

  pushd "${component_name}" >/dev/null
  pyinstaller "${component_spec}"
  popd >/dev/null

  local dist_path="${component_name}/dist/${component_artifact}"
  if [[ ! -e "${dist_path}" ]]; then
    local fallback_app="${component_name}/dist/${component_name}.app"
    local fallback_bin="${component_name}/dist/${component_name}"
    if [[ -e "${fallback_app}" ]]; then
      dist_path="${fallback_app}"
    elif [[ -e "${fallback_bin}" ]]; then
      dist_path="${fallback_bin}"
    else
      echo "[ERROR] ${component_name} artifact not found at ${dist_path}"
      return 1
    fi
  fi

  cp -R "${dist_path}" "${RELEASE_DIR}/"

  if [[ -d "${component_name}/build/${component_name}" ]]; then
    cp -R "${component_name}/build/${component_name}" "${RELEASE_DIR}/${component_build_label}"
  fi
}

build_component "analyzer" "kestrel_analyzer.app" "analyzer.spec" "analyzer_build"
build_component "visualizer" "visualizer.app" "visualizer.spec" "visualizer_build"

echo
printf "%s\n" "========================================"
printf "%s\n" "App build completed"
printf "%s\n" "========================================"
echo
printf "Artifacts in: %s\n" "${RELEASE_DIR}"
printf "Analyzer app: %s/kestrel_analyzer.app\n" "${RELEASE_DIR}"
printf "Visualizer app: %s/visualizer.app\n" "${RELEASE_DIR}"
