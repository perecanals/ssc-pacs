# Custom Orthanc image with a patched Folder Indexer plugin.
#
# The patch adds a `RemoveMissingFiles` config flag to the indexer. When set
# to `false`, the plugin skips the deleted-file cleanup pass, keeping files in
# Orthanc's index even when they disappear from disk. This is required for the
# `cold_path_cache` workflow where DICOM files are temporarily evicted and
# later restored to their original paths.
#
# Upstream source: https://orthanc.uclouvain.be/hg/orthanc-indexer/

# ---------------------------------------------------------------------------
# Stage 1: build the patched plugin
#
# NOTE: must match the runtime Orthanc image's OS so that libjsoncpp / libboost
# ABIs line up. orthancteam/orthanc:latest is currently Ubuntu 25.10.
# ---------------------------------------------------------------------------
FROM ubuntu:25.10 AS builder

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      ca-certificates \
      mercurial \
      patch \
      unzip \
      libboost-filesystem-dev \
      libboost-thread-dev \
      libboost-regex-dev \
      libboost-iostreams-dev \
      libboost-locale-dev \
      libboost-system-dev \
      libboost-date-time-dev \
      libjsoncpp-dev \
      libsqlite3-dev \
      libssl-dev \
      libcrypto++-dev \
      libpugixml-dev \
      libuuid1 \
      uuid-dev \
      zlib1g-dev \
      libdcmtk-dev \
      libwrap0-dev \
      libcharls-dev \
      liblua5.3-dev \
      libjpeg-dev \
      libpng-dev \
      libcivetweb-dev \
      libprotobuf-dev protobuf-compiler \
      libgtest-dev \
      python3 \
    && rm -rf /var/lib/apt/lists/*

COPY src/ /src/
WORKDIR /build

# Use the stable plugin release path: this pins ORTHANC_FRAMEWORK_DEFAULT_SOURCE=web
# and ORTHANC_FRAMEWORK_DEFAULT_VERSION=1.12.3 per CMakeLists.txt. The plugin's
# ApiVersion range is compatible with Orthanc 1.12.10 runtime.
RUN cmake /src \
      -DCMAKE_BUILD_TYPE=Release \
      -DALLOW_DOWNLOADS=ON \
      -DSTATIC_BUILD=OFF \
      -DUSE_SYSTEM_ORTHANC_SDK=OFF \
      -DORTHANC_PLUGIN_VERSION=1.0.0 \
      -DBUILD_UNIT_TESTS=OFF \
    && make -j"$(nproc)" OrthancIndexer \
    && ls -l libOrthancIndexer.so \
    && strip libOrthancIndexer.so

# ---------------------------------------------------------------------------
# Stage 2: overlay the patched plugin onto the official Orthanc image
# ---------------------------------------------------------------------------
# Pinned by digest for reproducibility (see maintenance workstream 02). To
# bump, pull a newer orthancteam/orthanc tag, run
#   docker image inspect orthancteam/orthanc:<tag> --format '{{index .RepoDigests 0}}'
# and replace the digest below. Also update the builder Ubuntu version
# (currently 25.10) if the base OS changes.
FROM orthancteam/orthanc@sha256:a055ccd31f7cd1e45ce98912e1c6e8e83b6a8f262f5b8fe6c88e2b6cd060fc57

COPY --from=builder /build/libOrthancIndexer.so \
     /usr/share/orthanc/plugins/libOrthancIndexer.so
