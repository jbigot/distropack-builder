# Run with rootless Docker or Podman

Nothing in a run needs root on the host: under a rootless engine the whole run, containers
included, has the invoking user's privileges, and what it writes belongs to that user.

## Rootless Docker

Install it as Docker documents (`dockerd-rootless-setuptool.sh install`). The installer creates a
`rootless` Docker context: once it is the current one (`docker context use rootless`), the builder
finds the engine with no setting. Otherwise, point it there:

```sh
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock
```

On Ubuntu 24.04 and later, AppArmor restricts the unprivileged user namespaces rootless Docker
needs: Docker's documentation gives the AppArmor profile to install for rootlesskit.

## Podman

The builder talks to Podman through its Docker-compatible API socket. Enable it for your user:

```sh
systemctl --user enable --now podman.socket
```

When no Docker engine is found, the builder uses `$XDG_RUNTIME_DIR/podman/podman.sock` by itself;
`--docker-host unix://$XDG_RUNTIME_DIR/podman/podman.sock` makes the choice explicit when Docker is
installed too.

On Ubuntu 26.04, a run through Podman's socket can fail while removing a container, with
`rootless netns: kill network process: permission denied`: AppArmor's profile for `pasta`,
Podman's network helper, refuses the signals Podman sends it
([Ubuntu bug 2154379](https://launchpad.net/bugs/2154379)). The fix is a rule allowing them in that
profile, proposed upstream on the
[passt-dev list](https://lists.passt.top/hyperkitty/list/passt-dev@passt.top/thread/YLFJL7XUAZI6BOI3JRDH6UXPAV33YOUV/);
until Ubuntu ships it, use rootless Docker on that release.

## Subordinate ids

Both engines map the users of a container onto the ranges `/etc/subuid` and `/etc/subgid` give the
invoking user. Most distributions create them with the user; `usermod --add-subuids` and
`--add-subgids` add one otherwise. The builder's own containers run as container root, which maps
to the invoking user, but the package managers inside the builders switch to other users.

## SELinux

The builder disables SELinux labelling for its containers, so that it can mount the project's
directories without relabelling them: nothing is needed on SELinux-enforcing hosts.

## Which engine is used

`--docker-host` (or `DISTROPACK_DOCKER_HOST`) overrides everything else; see
[Containers and engines](../explanation/containers.md) for the order in which the builder looks.
