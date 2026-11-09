# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

FROM debian:stable-slim

VOLUME /aptlyroot
VOLUME /aptlyout
VOLUME /data
VOLUME /project
VOLUME /downloads

# aptly, and uscan and mk-origtargz (devscripts), with what they need to download over HTTPS, check
# upstream signatures, read git repositories and repack tarballs. The project is mounted read-only
# and owned by whoever runs the engine: git may read any repository in it.
RUN apt-get update -y \
 && apt-get dist-upgrade -y \
 && apt-get install -y --no-install-recommends \
    aptly \
    ca-certificates \
    devscripts \
    git \
    gpgv \
    libwww-perl \
    libfile-dirlist-perl \
    libfile-touch-perl \
    liblwp-protocol-https-perl \
    sqop \
    tini \
    xz-utils \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /data
