#!/usr/bin/env bash
# Copy every MinGW DLL an executable needs into its own directory.
#
# `ldd` is unreliable for MinGW binaries under MSYS2, so dependencies are
# read with `objdump -p` and resolved transitively: a DLL pulled in by
# another DLL is just as required as a direct import, and missing one only
# shows up as a cryptic 0xc000007b at runtime on the target machine.
#
# System DLLs (kernel32, user32, …) are deliberately skipped — they are
# part of Windows and must not be shipped.
#
# Usage: tools/collect-dlls.sh bin/pubdump.exe [search-dir]

set -euo pipefail

BIN=${1:?usage: collect-dlls.sh <exe> [search-dir]}
SEARCH=${2:-/ucrt64/bin}
OUT=$(dirname "$BIN")

if ! command -v objdump >/dev/null; then
  echo "objdump not found; install mingw-w64-ucrt-x86_64-binutils" >&2
  exit 1
fi

declare -A seen=()
queue=("$BIN")
copied=0

while [ ${#queue[@]} -gt 0 ]; do
  current=${queue[0]}
  queue=("${queue[@]:1}")

  deps=$(objdump -p "$current" 2>/dev/null | awk '/DLL Name:/ {print $3}' || true)
  for dep in $deps; do
    [ -n "${seen[$dep]:-}" ] && continue
    seen[$dep]=1
    if [ -f "$SEARCH/$dep" ]; then
      cp -f "$SEARCH/$dep" "$OUT/"
      copied=$((copied + 1))
      echo "  bundled $dep"
      queue+=("$SEARCH/$dep")
    fi
  done
done

echo "collected $copied DLL(s) into $OUT"

# A binary that needed nothing from /ucrt64/bin almost certainly means the
# search directory is wrong, which would produce a bundle that fails only
# on the target machine.
if [ "$copied" -eq 0 ]; then
  echo "ERROR: no MinGW DLLs found - is $SEARCH correct?" >&2
  exit 1
fi
