#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FastNetMon Ltd.
#
# Build the timing and validation clients in an Ubuntu 24.04 container and
# extract both binaries to ./dist on the host. No host-side build
# toolchain required beyond docker.
#
# Runtime requirements on the target Ubuntu 24.04 machine (install before
# running the extracted binary):
#     sudo apt-get install -y libgrpc++1.51t64 libprotobuf32t64
#
# Usage:
#   ./build.sh                 # build + extract
#   ./build.sh --no-cache      # force clean rebuild
#   IMAGE=foo ./build.sh       # override image tag

set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-gobgp-client-benchmark:ubuntu-24.04}"
OUT_DIR="${OUT_DIR:-$PWD/dist}"
BUILD_ARGS=("$@")

echo "==> building image ${IMAGE}"
docker build "${BUILD_ARGS[@]}" -t "${IMAGE}" .

echo "==> extracting binary"
mkdir -p "${OUT_DIR}"
cid="$(docker create "${IMAGE}")"
trap 'docker rm -f "${cid}" >/dev/null 2>&1 || true' EXIT
docker cp "${cid}:/src/build/gobgp_client_benchmark" "${OUT_DIR}/gobgp_client_benchmark"
docker cp "${cid}:/src/build/gobgp_table_validate" "${OUT_DIR}/gobgp_table_validate"

echo
echo "=> ${OUT_DIR}/gobgp_client_benchmark"
echo "=> ${OUT_DIR}/gobgp_table_validate"
ls -la "${OUT_DIR}/gobgp_client_benchmark"
file "${OUT_DIR}/gobgp_client_benchmark" 2>/dev/null || true
echo
echo "target-machine runtime deps (Ubuntu 24.04):"
echo "  sudo apt-get install -y libgrpc++1.51t64 libprotobuf32t64"
