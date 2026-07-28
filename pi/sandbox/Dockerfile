# syntax=docker/dockerfile:1
# Pinned amd64 Node 22 image; Debian slim avoids a second Node/npm stack.
FROM node:22-bookworm-slim@sha256:8607a9064d4a571140998ae9e52a3b3fcf9cff361d04642d5971e6cd76d39e27

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
    && mkdir -p /home/sandbox/.local/bin /tmp/pi-home \
    && chown -R sandbox:sandbox /home/sandbox /tmp/pi-home \
    && rm -rf /var/lib/apt/lists/*

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
