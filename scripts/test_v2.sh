#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
python3 "$root/scripts/build_v2.py" --check
image="${MOVIEPILOT_V2_TEST_IMAGE:-jxxghp/moviepilot-v2@sha256:30b4de2a4e23da504f4a90ece710ed9f0787570063877bcc48e49febd340e3e8}"
docker run --rm --network none --entrypoint python \
  -e MOVIEPILOT_AUTO_UPDATE=false -e CONFIG_DIR=/tmp/mp-config \
  -v "$root:/repo:ro" \
  -v "$root/plugins.v2/ptsigninkeeper:/app/app/plugins/ptsigninkeeper:ro" \
  -v "$root/plugins.v2/ptinvitekeeper:/app/app/plugins/ptinvitekeeper:ro" \
  "$image" -B /repo/scripts/run_v2_tests.py
