# SPDX-FileCopyrightText: 2020-2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

ARG DIST_ID=fedora
ARG DIST_IMAGE=${DIST_ID}
ARG DIST_RELEASE=latest

FROM ${DIST_IMAGE}:${DIST_RELEASE}

VOLUME /src
VOLUME /output
VOLUME /localrepo

ARG DIST_ID
# 1 for Enterprise Linux: CRB (PowerTools on 8) and EPEL enabled
ARG DIST_EL=0

RUN EXTRA_PKGS="" \
  ; EXTRA_REPOS="" \
  ; case "${DIST_ID}" in \
    "fedora" ) \
      EXTRA_PKGS="nosync" \
  ;; \
    esac \
  ; if [ "${DIST_EL}" = 1 ] \
  ; then \
      EXTRA_REPOS="epel-release" \
  ; fi \
 && dnf upgrade -y \
 && dnf install -y --setopt=tsflags=nodocs --setopt=install_weak_deps=False --best \
    'dnf-command(builddep)' \
    openssl \
    rpm-build \
    shadow-utils \
    util-linux \
    ${EXTRA_PKGS} \
 && if [ -n "${EXTRA_REPOS}" ] \
  ; then \
      dnf config-manager --set-enabled crb 2>/dev/null \
      || dnf config-manager --set-enabled powertools \
  &&  dnf install -y --setopt=tsflags=nodocs ${EXTRA_REPOS} \
  ; fi \
 && dnf upgrade -y \
 && dnf clean all -y --enablerepo='*' \
 && rm -f /*.log ${HOME}/* \
 && useradd --create-home builder

ENTRYPOINT ["/bin/bash", "-l", "/entrypoint"]

COPY entrypoint /
