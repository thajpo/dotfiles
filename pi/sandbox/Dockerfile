# syntax=docker/dockerfile:1
# Pinned multi-architecture Node 22 image; Debian slim avoids a second
# Node/npm stack while Docker resolves the native Linux platform.
FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/home/sandbox/.local/bin:$PATH

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       bash ca-certificates curl jq git ripgrep fd-find \
       python3 python3-pip python3-venv \
       build-essential pkg-config cmake ninja-build \
       tar gzip bzip2 xz-utils zstd zip unzip file openssh-client \
       procps less \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && useradd --create-home --shell /bin/bash sandbox \
    && mkdir -p /home/sandbox/.local/bin /tmp/pi-home /var/cache/pi-packages \
    && chmod 1777 /var/cache/pi-packages \
    && chown -R sandbox:sandbox /home/sandbox /tmp/pi-home \
    && rm -rf /var/lib/apt/lists/*

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
