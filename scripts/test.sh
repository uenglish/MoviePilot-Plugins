#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
for plugin in ptsigninkeeper ptinvitekeeper; do
  (cd "$root/plugins.v3/$plugin/frontend" && npm ci --ignore-scripts && npm test && npm run check && npm run build)
done
image="${MOVIEPILOT_TEST_IMAGE:-jxxghp/moviepilot-v3@sha256:51cf8fe90a474bde2d79bbbccb5ce83f926b4da887b6fcc7c9457730095d6ebb}"
docker run --rm --network none --entrypoint /bin/sh \
  -e MOVIEPILOT_AUTO_UPDATE=false -e CONFIG_DIR=/tmp/mp-config \
  -v "$root/plugins.v3/ptsigninkeeper:/app/app/plugins/ptsigninkeeper:ro" \
  -v "$root/plugins.v3/ptinvitekeeper:/app/app/plugins/ptinvitekeeper:ro" \
  -v "$root/tests/v3:/plugin-tests:ro" "$image" -c \
  'cd /app; rc=0; for p in ptsigninkeeper ptinvitekeeper; do python -B -m unittest discover -s /plugin-tests/$p -p "test_*.py" || rc=1; done; exit $rc'
python3 "$root/scripts/check_release.py"
