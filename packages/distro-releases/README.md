# distro-releases

The releases of Linux distributions, with their dates, their archive suites and their support
status, from each distribution's own data:

| Distribution | id | Source |
|---|---|---|
| AlmaLinux OS | `almalinux` | endoflife.date |
| Debian GNU/Linux | `debian` | distro-info-data |
| Fedora | `fedora` | Bodhi, Fedora's update system |
| Red Hat Enterprise Linux | `rhel` | endoflife.date |
| Rocky Linux | `rocky` | endoflife.date |
| Ubuntu | `ubuntu` | distro-info-data |

A source is read the first time a release of its distribution is asked for, never at import.

## Install

```sh
pip install distro-releases
```

## Use

```python
from distro_releases import distribution, select

debian = distribution('debian')
for release in debian.releases():
    if release.is_supported():
        print(release, release.suite)  # Debian GNU/Linux 13 (Trixie) stable

select(debian, ['bookworm', '+'])  # bookworm and every later release
```

```console
$ distro-releases list debian ubuntu:noble,+
DISTRIBUTION  VERSION  CODENAME  SUITE   RELEASED    EOL         STATUS
debian        13       trixie    stable  2025-08-09  2028-08-09  supported
...
```

The documentation is at https://jbigot.github.io/distropack-builder/.
