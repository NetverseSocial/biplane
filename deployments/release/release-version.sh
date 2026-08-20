#!/usr/bin/env bash
# Shim. THE authority lives with the datum, the corpus and the Python side in
# apps/api/plane/license/utils/ — together, because the API image copies only
# apps/api and a parity test that cannot reach both implementations is not a
# parity test (Rowan RC 3493). Release scripts source this path; it forwards.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../apps/api/plane/license/utils/release_version.sh"
