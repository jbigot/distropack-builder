# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

FROM fedora:latest

VOLUME /data

RUN dnf upgrade -y \
 && dnf install -y --setopt=tsflags=nodocs --setopt=install_weak_deps=False \
    gnupg2 \
    rpm-sign \
    tini \
 && dnf clean all -y \
 && rm -f /*.log ${HOME}/*

COPY sign /usr/local/bin/sign
ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /data
