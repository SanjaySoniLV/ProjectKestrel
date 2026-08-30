#!/usr/bin/env bash
#
# Vendor pyexiv2's external Homebrew dependencies into its own lib/ directory.
#
# THE PROBLEM
#
# pyexiv2's macOS wheel ships pyexiv2/lib/libexiv2.dylib, but that dylib was
# never delocated: its load commands still point at the Homebrew prefix of the
# machine it was built on.
#
#   $ otool -L .../pyexiv2/lib/libexiv2.dylib
#     /opt/homebrew/opt/inih/lib/libINIReader.0.dylib
#     /opt/homebrew/opt/inih/lib/libinih.0.dylib
#     /opt/homebrew/opt/brotli/lib/libbrotlidec.1.dylib
#     /opt/homebrew/opt/brotli/lib/libbrotlicommon.1.dylib
#     /opt/homebrew/opt/gettext/lib/libintl.8.dylib
#
# Homebrew does not exist on a typical user's Mac, so `import pyexiv2` raises
# OSError there and the opt-in "embed XMP into JPEG originals" feature fails on
# every file — silently, because metadata_writer._load_pyexiv2() degrades to a
# per-file embed_errors entry rather than raising. The user gets no metadata and
# no meaningful explanation.
#
# WHY NOT delocate
#
# delocate is the obvious tool and it does not work here. pyexiv2/lib/__init__.py
# loads the library with an explicit ctypes.CDLL(<lib_dir>/libexiv2.dylib)
# preload, and libexiv2.dylib's install name is @rpath/libexiv2.28.dylib. The
# preload is what makes that @rpath reference resolve at runtime — the image is
# already loaded under that name by the time exiv2api.so needs it. There is no
# file named libexiv2.28.dylib on disk and exiv2api.so carries no LC_RPATH, so
# delocate's static graph walk fails with "@rpath/libexiv2.28.dylib not found"
# before it ever gets to the dependencies that actually need fixing.
#
# Since only libexiv2.dylib has bad load commands, rewriting them directly is
# both sufficient and more predictable than fighting delocate's model.
#
# WHAT THIS DOES
#
# Walk libexiv2.dylib's dependency graph. For every dependency under a Homebrew
# prefix, copy it next to libexiv2.dylib and rewrite the reference to
# @loader_path/<name>, recursing so transitive deps (libINIReader -> libinih,
# libbrotlidec -> libbrotlicommon) are handled too. /usr/lib/* is left alone:
# those ship with macOS.
#
# Files land directly in pyexiv2/lib/ so PyInstaller's collect_all('pyexiv2')
# picks them up as ordinary package binaries.
#
# Usage: packaging/vendor_macos_native_deps.sh <path-to-venv-python>

set -euo pipefail

PY="${1:?usage: vendor_macos_native_deps.sh <path-to-venv-python>}"

# Homebrew prefixes differ by arch: /opt/homebrew on arm64, /usr/local on x86_64.
# Match both rather than hardcoding, so this works on the Intel runner too.
BREW_RE='^(/opt/homebrew|/usr/local)/'

echo "==> Installing the Homebrew formulae libexiv2 links against"
for formula in inih brotli gettext; do
  brew list "$formula" >/dev/null 2>&1 || brew install "$formula"
done

# Locate pyexiv2 WITHOUT importing it — importing triggers the broken dlopen.
LIB_DIR="$("$PY" -c 'import sysconfig, os; print(os.path.join(sysconfig.get_paths()["purelib"], "pyexiv2", "lib"))')"
[[ -d "$LIB_DIR" ]] || { echo "ERROR: pyexiv2 lib dir not found at $LIB_DIR" >&2; exit 1; }
echo "==> pyexiv2 lib dir: $LIB_DIR"

echo "==> BEFORE:"
otool -L "$LIB_DIR/libexiv2.dylib" | sed -n '2,30p'

# Re-signing is not optional. On Apple Silicon every binary needs a valid
# signature, and install_name_tool invalidates it — an unsigned-after-edit dylib
# is killed by dyld at load time. Ad-hoc (-) signing is what the later real
# signing step expects to find.
resign() {
  codesign --force --sign - "$1" 2>/dev/null || true
}

# Breadth-first over the dependency graph, since the Homebrew libs depend on
# each other (libINIReader -> libinih, libbrotlidec -> libbrotlicommon).
process_queue=("$LIB_DIR/libexiv2.dylib")
declare -a copied=()

while [[ ${#process_queue[@]} -gt 0 ]]; do
  current="${process_queue[0]}"
  process_queue=("${process_queue[@]:1}")
  [[ -f "$current" ]] || continue

  # Skip the leading line of otool -L (the file's own path) and the install-name
  # line, then take the path column of each dependency.
  while read -r dep; do
    [[ -n "$dep" ]] || continue
    [[ "$dep" =~ $BREW_RE ]] || continue          # /usr/lib/* ships with macOS

    base="$(basename "$dep")"
    dest="$LIB_DIR/$base"

    if [[ ! -f "$dest" ]]; then
      if [[ ! -f "$dep" ]]; then
        echo "ERROR: dependency $dep does not exist on this machine" >&2
        exit 1
      fi
      echo "    copying $base"
      cp "$dep" "$dest"
      chmod u+w "$dest"
      # Its own id must not point back at the Homebrew prefix either.
      install_name_tool -id "@loader_path/$base" "$dest"
      resign "$dest"
      copied+=("$base")
      process_queue+=("$dest")                    # recurse into its deps
    fi

    echo "    $(basename "$current"): $dep -> @loader_path/$base"
    chmod u+w "$current"
    install_name_tool -change "$dep" "@loader_path/$base" "$current"
    resign "$current"
  done < <(otool -L "$current" | tail -n +2 | awk '{print $1}')
done

echo "==> Copied ${#copied[@]} libraries: ${copied[*]:-none}"

echo "==> AFTER:"
otool -L "$LIB_DIR/libexiv2.dylib" | sed -n '2,30p'

# Any surviving absolute Homebrew path means the app would still break off-CI,
# so fail here rather than shipping it. Check every binary, not just libexiv2.
echo "==> Verifying no Homebrew paths remain"
leaked=0
for f in "$LIB_DIR"/*.dylib "$LIB_DIR"/*.so; do
  [[ -e "$f" ]] || continue
  if otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -qE "$BREW_RE"; then
    echo "ERROR: $(basename "$f") still references a Homebrew path:" >&2
    otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -E "$BREW_RE" >&2
    leaked=1
  fi
done
[[ $leaked -eq 0 ]] || exit 1

# The real proof: this is the exact import that fails on a machine without
# Homebrew. Do not let a build reach signing or upload if it cannot load.
echo "==> Verifying pyexiv2 imports"
"$PY" -c "import pyexiv2; print('pyexiv2 OK:', pyexiv2.__version__, '/ exiv2', pyexiv2.__exiv2_version__)"

echo "==> pyexiv2 native dependencies vendored successfully."
