# depotctl uploader: steamcmd + rcodesign on Debian (amd64; steamcmd is an x86 binary).
FROM --platform=linux/amd64 debian:bookworm-slim

ARG RCODESIGN_VERSION=0.29.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl lib32gcc-s1 lib32stdc++6 locales tar \
 && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# rcodesign: ad-hoc signs macOS .app bundles from Linux
RUN curl -fsSL "https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F${RCODESIGN_VERSION}/apple-codesign-${RCODESIGN_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C /tmp \
 && mv /tmp/apple-codesign-*/rcodesign /usr/local/bin/rcodesign \
 && rm -rf /tmp/apple-codesign-* \
 && rcodesign --version

# (plain COPY + chmod so older Docker versions without BuildKit, e.g. Debian 12's docker.io, work)
COPY depotctl-steam.sh /usr/local/bin/depotctl-steam
RUN chmod 755 /usr/local/bin/depotctl-steam

RUN useradd --create-home --uid 1000 steam
USER steam
WORKDIR /home/steam

# /home/steam is mounted as a named volume; Docker seeds it from this layer on first use,
# and it keeps steamcmd's self-updates and the cached login between runs.
RUN mkdir -p steamcmd \
 && curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xz -C steamcmd \
 && (./steamcmd/steamcmd.sh +quit || true)

ENTRYPOINT ["depotctl-steam"]
