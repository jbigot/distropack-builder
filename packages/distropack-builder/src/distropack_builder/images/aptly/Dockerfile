# SPDX-FileCopyrightText: 2025-2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

FROM debian:stable-slim

VOLUME /aptlyroot
VOLUME /aptlyout
VOLUME /data

RUN apt-get update -y \
 && apt-get dist-upgrade -y \
 && apt-get install -y --no-install-recommends \
    aptly \
    tini \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /data
