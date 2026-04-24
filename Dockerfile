# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FastNetMon Ltd.
#
# Build-only image. Produces /src/build/gobgp_client_benchmark.
# The build binary dynamically links against libgrpc++1.51t64 and libprotobuf32t64
# from Ubuntu 24.04 — install those on the target to run.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake \
        g++ \
        make \
        pkg-config \
        libgrpc++-dev \
        libprotobuf-dev \
        protobuf-compiler \
        protobuf-compiler-grpc \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt main.cpp ./
COPY proto ./proto

RUN cmake -B build -S . -Wno-dev \
 && cmake --build build -j"$(nproc)" \
 && strip build/gobgp_client_benchmark

# Default command prints build info when run without an override.
CMD ["/src/build/gobgp_client_benchmark", "--help"]
