<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# The images

The builder runs every tool in an image from `ghcr.io/jbigot/distropack-builder/`, all public and
defined by `distropack-build images bake-file` alone (see [](../how-to/build-images.md)).

## Names and tags

The tool images, `deb_tools` and `rpm_sign`, are tagged `latest` and `p<N>`.

There is one builder image per release of each distribution that has not reached its end of life,
or that its distribution's archive still supports for free after it (Debian's LTS), named
`<distribution>_builder`: `debian_builder`, `ubuntu_builder`, `fedora_builder`, `el_builder` (from
Rocky Linux's images), `opensuse_builder` (zypper's variant, from openSUSE's Leap and Tumbleweed
images). It carries these tags:

| tag | example | present |
|---|---|---|
| the release: its codename in the Debian family, its version elsewhere | `trixie`, `44` | always |
| the protocol tag, `<release>-p<N>` | `trixie-p2`, `44-p2` | always |
| the codename, where it differs from the release tag | none today | when the release has one |
| the suite | `stable`, `rawhide`, `devel` | when the release has one |
| `latest` | `trixie`, `45` | on the newest supported release |

## The protocol version

`<N>`, 3 today, is the version of what the runner relies on in an image: its entrypoint and its
volumes. It is bumped whenever either changes incompatibly, so that a runner can ask for an image
it can drive: the runner asks for the protocol tag of each image, never a moving tag. Protocol 2
(the builders install their own build dependencies, answer the rpm spec queries, and build as an
unprivileged user) replaced the tool images `deb_tools`, `rpm_tools` and `gpg2` by `aptly` and
`rpm_sign`; `deb_tools` came back in place of `aptly` with uscan beside it, one image being smaller
than two that share their base. Protocol 3 splits a build in two containers, the builders'
entrypoints taking a mode: `deps` installs the build dependencies (with network) before the runner
commits the container, `build` builds from that commit (without network), and the rpm builders'
`query` answers the spec queries.

## Availability

A release is available exactly when its protocol tag exists in the registry. A release whose base
image its registry does not publish yet, such as a development release just opened, gets no image
and no tag; it gets them on the first build after its base image appears.

## Rebuilds

The `Docker images` workflow builds every image on each change to the images or their definition,
and nightly, so that they follow their distributions' updates. Only a run on `main` publishes.
