#!/usr/bin/env bash
# Build the release zip from git, with a VERSION file (the ledger records it for every session).
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-arth_repo.zip}
git archive --format=zip --prefix=arth/ -o "$out" HEAD
python3 - "$out" "$(git describe --tags --always)" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "a") as z:
    z.writestr("arth/VERSION", sys.argv[2] + "\n")
PY
echo "$out ($(git describe --tags --always))"
