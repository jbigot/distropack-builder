# distropack-builder

Tools to build deb and rpm packages & repositories.

This repository is a uv workspace of two packages:

- `packages/distro-releases/`: the releases of each Linux distribution and their support status;
- `packages/distropack-builder/`: the builder itself, with its Docker images under
  `src/distropack_builder/images/`.

Run the builder from a project directory holding a `build.conf`:

```sh
uv run --project <path to this repository> distropack-build run build.conf
```

`python -m distropack_builder` is equivalent. `./build` builds the Docker images.
