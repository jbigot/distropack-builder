<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Check that your packages build reproducibly

```sh
distropack-build run --check-reproducible
```

or, in the GitHub Action, `check-reproducible: true`. Every package is built twice, varied, and a
package whose files differ fails as `unreproducible`:

```text
unreproducible hello on Fedora 44: hello-1.0-1.fc44.noarch.rpm, hello-1.0-1.fc44.src.rpm
```

Its output holds diffoscope's report on each differing file (three at most), which says what
differs: a timestamp in a file, an rpm header field, the order of a list. For an rpm package, set
first what Fedora's rpm leaves off by default, in the spec:

```spec
%global clamp_mtime_to_source_epoch 1
%global use_source_date_epoch_as_buildtime 1
```

The check doubles the build time; run it in a scheduled job rather than on every push. See
[reproducible builds](../explanation/reproducibility.md) for what the builder fixes and what is
left to the package.
