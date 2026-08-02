#!/usr/bin/env bash
#
# Vendor pyexiv2's external native dependencies into its own package directory.
#
# WHY THIS EXISTS
#
# pyexiv2's macOS wheel ships its own libexiv2.dylib, but that dylib carries an
# ABSOLUTE load path to Homebrew's libINIReader:
#
#     /opt/homebrew/opt/inih/lib/libINIReader.0.dylib      (arm64)
#     /usr/local/opt/inih/lib/libINIReader.0.dylib         (x86_64)
#
# The wheel was never delocated, so that path is baked in. Homebrew does not
# exist on a typical user's Mac, so `import pyexiv2` raises
#
#     OSError: dlopen(...libexiv2.dylib): Library not loaded: .../libINIReader.0.dylib
#
# and the opt-in "embed XMP into JPEG originals" feature fails on every file —
# silently, because metadata_writer._load_pyexiv2() degrades to a per-file
# embed_errors entry rather than crashing. The user just gets no metadata.
#
# PyInstaller's collect_all('pyexiv2') copies the package's own files, but it
# does not rewrite external load commands and cannot collect a library that is
# not part of the wheel. So the repair has to happen in the venv, BEFORE
# PyInstaller freezes it and before anything is signed.
#
# delocate is the tool wheel builders use for exactly this: it walks the load
# commands, copies each external dependency in beside the library, and rewrites
# the paths to be relative (@loader_path). Homebrew's inih must be installed
# first, because delocate can only copy a dependency that actually exists.
#
# Usage: packaging/vendor_macos_native_deps.sh <path-to-venv-python>

set -euo pipefail

PY="${1:?usage: vendor_macos_native_deps.sh <path-to-venv-python>}"
BIN_DIR="$(dirname "$PY")"

# The full set of Homebrew libraries libexiv2.dylib links against, from otool -L
# on a macos-latest runner:
#   /opt/homebrew/opt/inih/lib/libINIReader.0.dylib
#   /opt/homebrew/opt/inih/lib/libinih.0.dylib
#   /opt/homebrew/opt/brotli/lib/libbrotlidec.1.dylib
#   /opt/homebrew/opt/brotli/lib/libbrotlicommon.1.dylib
#   /opt/homebrew/opt/gettext/lib/libintl.8.dylib
# (/usr/lib/* are OS-provided and always present, so they need no vendoring.)
# delocate can only copy a dependency that exists on disk, so install all three
# formulae even though the runner usually already has brotli and gettext.
echo "==> Installing Homebrew deps that libexiv2 links against"
for formula in inih brotli gettext; do
  brew list "$formula" >/dev/null 2>&1 || brew install "$formula"
done

echo "==> Installing delocate"
"$PY" -m pip install --quiet delocate

# Locate pyexiv2 WITHOUT importing it — importing triggers the very dlopen that
# is broken until this script has run.
LIB_DIR="$("$PY" -c 'import sysconfig, os; print(os.path.join(sysconfig.get_paths()["purelib"], "pyexiv2", "lib"))')"
if [[ ! -d "$LIB_DIR" ]]; then
  echo "ERROR: pyexiv2 lib directory not found at $LIB_DIR" >&2
  exit 1
fi
echo "==> pyexiv2 lib dir: $LIB_DIR"

echo "==> Load commands BEFORE:"
otool -L "$LIB_DIR"/libexiv2.dylib | sed -n '2,20p' || true

# -L vendored, not the default ".dylibs": a dot-prefixed directory is easy for
# packaging tools to treat as hidden and skip, and this has to survive
# PyInstaller's collect_all. (The flag is -L / --lib-path; delocate-path has no
# --lib-sdir, which is a delocate-wheel option.)
if [[ -x "$BIN_DIR/delocate-path" ]]; then
  "$BIN_DIR/delocate-path" -L vendored "$LIB_DIR"
else
  "$PY" -m delocate.cmd.delocate_path -L vendored "$LIB_DIR"
fi

echo "==> Load commands AFTER:"
otool -L "$LIB_DIR"/libexiv2.dylib | sed -n '2,20p' || true

echo "==> Vendored libraries:"
ls -la "$LIB_DIR/vendored" 2>/dev/null || echo "  (none copied — see the check below)"

# Fail the build here rather than shipping a silently broken feature. This is
# the exact import that fails on a machine without Homebrew.
echo "==> Verifying pyexiv2 imports"
"$PY" - <<'PYEOF'
import pyexiv2
print("pyexiv2 OK:", getattr(pyexiv2, "__version__", "unknown"))
PYEOF

# A remaining absolute Homebrew path means delocate did not fully resolve the
# graph, and the app would still break off-CI even though the import worked here.
if otool -L "$LIB_DIR"/libexiv2.dylib | grep -qE '/(opt/homebrew|usr/local)/'; then
  echo "ERROR: libexiv2.dylib still references an absolute Homebrew path;" >&2
  echo "       the shipped app would fail on a Mac without Homebrew." >&2
  otool -L "$LIB_DIR"/libexiv2.dylib | grep -E '/(opt/homebrew|usr/local)/' >&2
  exit 1
fi

echo "==> pyexiv2 native dependencies vendored successfully."
