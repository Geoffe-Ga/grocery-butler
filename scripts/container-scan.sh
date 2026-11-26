#!/usr/bin/env bash
# scripts/container-scan.sh - Build the production image and scan it for
# known vulnerabilities.
#
# Fails on a HIGH or CRITICAL finding that has a fix. Findings with no fix
# yet are left out: there is nothing to do about them but wait.
#
# This is not part of ./scripts/check-all.sh or the CI gate. It runs nightly
# and when the image's inputs change (.github/workflows/container-scan.yml),
# so a new CVE in the base image is seen within a day without blocking
# unrelated work. Runbook: README "Container scanning".
#
# Usage: ./scripts/container-scan.sh [--verbose]
#
# Needs a running Docker daemon. The scanner is itself run as a container,
# pinned by version below, so nothing else has to be installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

[[ "${1:-}" == "--verbose" ]] && set -x

# Pinned like every other tool here. Upgrade deliberately: change the tag,
# run this script, commit.
TRIVY_IMAGE="ghcr.io/aquasecurity/trivy:0.76.1"
IMAGE="grocery-butler:container-scan"
SEVERITIES="HIGH,CRITICAL"

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker is not installed. Install Docker to scan the image." >&2
    exit 2
fi

echo "=== Container scan ==="
docker build --quiet --tag "$IMAGE" . >/dev/null \
    || { echo "✗ The image did not build." >&2; exit 1; }

docker run --rm \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    "$TRIVY_IMAGE" image \
    --severity "$SEVERITIES" \
    --ignore-unfixed \
    --exit-code 1 \
    --no-progress \
    "$IMAGE" \
    || { echo "✗ The image has fixable $SEVERITIES findings (or the scan could not run). See above." >&2; exit 1; }

echo "✓ No fixable $SEVERITIES findings in the image"
