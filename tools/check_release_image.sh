#!/usr/bin/env bash
# Check a single release platform image and write its SBOM.
set -euo pipefail
: "${IMAGE:?IMAGE must name the release image by digest}"
: "${SBOM:?SBOM must be the SBOM output path}"
: "${PLATFORM:?PLATFORM must be a linux platform such as linux/arm64}"
docker pull --platform "$PLATFORM" "$IMAGE"
bash "$(dirname "$0")/check_container.sh" "$IMAGE"
bash "$(dirname "$0")/audit_image.sh" "$IMAGE" "$SBOM" "$PLATFORM"
