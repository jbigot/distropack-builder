# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

# The rpm builder of the distributions whose package manager is zypper (openSUSE).

ARG DIST_IMAGE=opensuse/leap
ARG DIST_RELEASE=latest

FROM ${DIST_IMAGE}:${DIST_RELEASE}

VOLUME /src
VOLUME /output
VOLUME /localrepo

# openSUSE has no %{?dist}: the release's own tag (lp160, tw), so that each release's packages are
# versioned apart, as Fedora's and Enterprise Linux's are
ARG DIST_TAG

RUN zypper --non-interactive refresh \
 && zypper --non-interactive dist-upgrade --no-recommends \
 && zypper --non-interactive install --no-recommends \
    rpm-build \
    shadow \
    util-linux \
 && zypper --non-interactive clean --all \
 && printf '%%dist .%s\n' "${DIST_TAG}" > /etc/rpm/macros.dist \
 && useradd --create-home builder

ENTRYPOINT ["/bin/bash", "-l", "/entrypoint"]

COPY entrypoint /
