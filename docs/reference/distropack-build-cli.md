<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# The `distropack-build` command

Every expected error ends the command with one `distropack-build: error:` line on standard error:
status 2 when the command line or `build.conf` asks for something wrong, 1 when something failed
while doing it (a release source or the registry not answering, a key that does not unlock, a
build or a publication that failed). An interrupted run (Ctrl-C, `SIGTERM`) cleans up and exits
with status 130.

The passphrase of the signing key never comes from the command line: `run` reads it from the file
`--passphrase-file` names, or else from the `DISTROPACK_GPG_PASSPHRASE` environment variable. The
file's final line ending is not part of the passphrase.

```{eval-rst}
.. typer:: distropack_builder.cli:app
   :prog: distropack-build
   :preferred: text
   :show-nested:
   :width: 100
```
