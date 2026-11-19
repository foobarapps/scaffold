#!/bin/sh
# Prints the section of a version in CHANGELOG.md: everything from its heading up to the
# next version's heading. Fails if there is none, so that nothing is released without notes.
#
# Usage: release-notes.sh v0.10.0
set -eu

version="${1#v}"

notes="$(awk -v heading="## ${version}" '
  $0 == heading { found = 1; next }
  found && /^## / { exit }
  found { print }
' CHANGELOG.md)"

if [ -z "$notes" ]; then
  echo "CHANGELOG.md has no section for ${version}" >&2
  exit 1
fi

printf '%s\n' "$notes"
