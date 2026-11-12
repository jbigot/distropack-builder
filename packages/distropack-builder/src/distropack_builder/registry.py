# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The states of releases in an OCI registry: one artifact per (channel, release), one tar layer.

A tag `<channel>.<release uid>` names the artifact; the registry moves it only once the push is
complete, so that an interrupted push leaves the previous state, and the previous versions stay
as untagged ones, which `tag` puts back. A tag the registry does not have is "no state"; any other
failure (the network, a refused token, a registry error) is an error, never an empty state: that
would restart the rebuild numbers.

oras-py does the transfers. Its credentials are Docker's (`~/.docker/config.json`, its credential
stores and helpers, as `docker login` and `docker/login-action` write them), unless a token is
given.
"""

import hashlib
import tempfile
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import oras.provider
import requests

from distropack_builder import state

ARTIFACT_TYPE = 'application/vnd.distropack.state.v1'
LAYER_TYPE = 'application/vnd.oci.image.layer.v1.tar'
MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
# The registries' error codes of a repository or tag that does not exist.
_MISSING = {'MANIFEST_UNKNOWN', 'NAME_UNKNOWN', 'NOT_FOUND'}


class RegistryError(Exception):
    """The registry could not be reached, refused the credentials, or failed: never "no state"."""


def tag_of(channel: str, uid: str) -> str:
    """The tag of the state of uid on channel."""
    return f'{channel}.{uid}'


def annotations(environ: Mapping[str, str]) -> dict[str, str]:
    """The annotations of a push from GitHub Actions, linking the artifact to its repository."""
    found: dict[str, str] = {}
    if environ.get('GITHUB_SERVER_URL') and environ.get('GITHUB_REPOSITORY'):
        source = f'{environ["GITHUB_SERVER_URL"]}/{environ["GITHUB_REPOSITORY"]}'
        found['org.opencontainers.image.source'] = source
    if environ.get('GITHUB_SHA'):
        found['org.opencontainers.image.revision'] = environ['GITHUB_SHA']
    if environ.get('GITHUB_RUN_ID'):
        found['dev.distropack.run-id'] = environ['GITHUB_RUN_ID']
    return found


class Registry:
    """A repository of a registry holding states, `<registry host>/<path>`."""

    def __init__(self, reference: str, token: str | None = None, insecure: bool = False) -> None:
        """The repository reference; token, if given, instead of Docker's credentials."""
        self.reference = reference.removesuffix('/')
        host = self.reference.split('/', 1)[0]
        self.client = oras.provider.Registry(hostname=host, insecure=insecure)
        if token is not None:
            self.client.auth.set_basic_auth('distropack', token)
            # oras-py reads Docker's credentials again at each call: the token given wins
            self.client.auth.load_configs = lambda *args, **kwargs: None

    def _manifest(self, ref: str) -> dict[str, Any] | None:
        """The manifest named ref (a tag or a digest), None when the registry has none."""
        container = self.client.get_container(f'{self.reference}{ref}')
        self.client.auth.load_configs(container)
        url = f'{self.client.prefix}://{container.manifest_url()}'
        try:
            response = self.client.do_request(url, 'GET', headers={'Accept': MANIFEST_TYPE})
        except Exception as error:
            raise RegistryError(f'{self.reference}{ref}: {_reason(error)}') from error
        if response.status_code == 404 or _codes(response) & _MISSING:
            return None
        if response.status_code != 200:
            raise RegistryError(f'{self.reference}{ref}: {_status(response)}')
        manifest: dict[str, Any] = response.json()
        return manifest

    def pull(self, channel: str, uids: Sequence[str], states: Path) -> list[str]:
        """Make each release's state the one the registry has; the tags it has not.

        Raises:
            RegistryError: the registry could not answer, or the layer is not a state.
        """
        missing: list[str] = []
        for uid in uids:
            tag = tag_of(channel, uid)
            manifest = self._manifest(f':{tag}')
            if manifest is None:
                missing.append(tag)
                continue
            layers = manifest.get('layers') or []
            if len(layers) != 1:
                raise RegistryError(f'{self.reference}:{tag} is not a state: {len(layers)} layers')
            digest = layers[0]['digest']
            with tempfile.TemporaryDirectory(prefix='distropack-state.') as scratch:
                layer = Path(scratch) / 'layer.tar'
                container = self.client.get_container(f'{self.reference}:{tag}')
                try:
                    self.client.download_blob(container, digest, str(layer))
                except Exception as error:
                    raise RegistryError(f'{self.reference}:{tag}: {_reason(error)}') from error
                with layer.open('rb') as data:
                    found = 'sha256:' + hashlib.file_digest(data, 'sha256').hexdigest()
                if found != digest:
                    raise RegistryError(f'{self.reference}:{tag}: the layer is not {digest}')
                try:
                    state.unpack(layer, states, channel, uid)
                except state.StateArchiveError as error:
                    raise RegistryError(f'{self.reference}:{tag}: {error}') from error
        return missing

    def push(self, channel: str, uid: str, states: Path, annotations: Mapping[str, str]) -> str:
        """Push the state of uid on channel under its tag; the manifest's digest.

        Raises:
            RegistryError: the push did not complete; the tag still names the previous state.
        """
        tag = tag_of(channel, uid)
        with tempfile.TemporaryDirectory(prefix='distropack-state.') as scratch:
            layer = Path(scratch) / f'{tag}.tar'
            try:
                state.pack(states, channel, uid, layer)
            except state.StateArchiveError as error:
                raise RegistryError(f'no state for {tag}: {error}') from error
            config = Path(scratch) / 'config.json'
            config.write_text('{}')
            try:
                response = self.client.push(
                    target=f'{self.reference}:{tag}',
                    files=[f'{layer}:{LAYER_TYPE}'],
                    manifest_config=f'{config}:{ARTIFACT_TYPE}',
                    manifest_annotations=dict(annotations),
                    disable_path_validation=True,
                    quiet=True,
                )
            except Exception as error:
                raise RegistryError(f'{self.reference}:{tag}: {_reason(error)}') from error
        digest = response.headers.get('Docker-Content-Digest')
        if not digest:
            raise RegistryError(f'{self.reference}:{tag}: the registry named no digest')
        return str(digest)

    def tag(self, digest: str, tag: str) -> None:
        """Make tag name the artifact digest, an older version of a state: a rollback.

        Raises:
            RegistryError: the registry has no such digest, or failed.
        """
        manifest = self._manifest(f'@{digest}')
        if manifest is None:
            raise RegistryError(f'{self.reference}@{digest}: no such artifact')
        container = self.client.get_container(f'{self.reference}:{tag}')
        # the token of the read may be scoped to pulling: the push asks its own
        self.client.auth.token = None
        try:
            response = self.client.upload_manifest(manifest, container)
        except Exception as error:
            raise RegistryError(f'{self.reference}:{tag}: {_reason(error)}') from error
        if response.status_code not in (200, 201, 202):
            raise RegistryError(f'{self.reference}:{tag}: {_status(response)}')


def _codes(response: requests.Response) -> set[str]:
    """The error codes of a registry's answer (the distribution API's `errors`)."""
    try:
        errors = response.json().get('errors') or []
    except (ValueError, AttributeError):
        return set()
    return {str(error.get('code')) for error in errors if isinstance(error, dict)}


def _status(response: requests.Response) -> str:
    """A registry's failed answer, in a few words."""
    try:
        errors = response.json().get('errors') or []
        messages = [str(e.get('message') or e.get('code')) for e in errors if isinstance(e, dict)]
    except (ValueError, AttributeError):
        messages = []
    detail = f': {"; ".join(messages)}' if messages else ''
    return f'{response.status_code} {response.reason}{detail}'


def _reason(error: Exception) -> str:
    """An error of a transfer, on one line."""
    return ' '.join(str(error).split()) or type(error).__name__


GITHUB_API = 'https://api.github.com'


def prune(reference: str, token: str, keep_untagged: int, client: httpx.Client) -> list[int]:
    """Delete the untagged versions of a ghcr package beyond the keep_untagged most recent.

    A tagged version is never deleted. The OCI API cannot list untagged manifests: GitHub's
    Packages API does, for an organisation's package or a user's.

    Returns:
        The ids of the versions deleted.

    Raises:
        ValueError: the registry is not ghcr.io.
        RegistryError: the API failed.
    """
    host, _, path = reference.removesuffix('/').partition('/')
    if host != 'ghcr.io' or '/' not in path:
        raise ValueError("--keep-untagged needs ghcr.io's API")
    owner, _, name = path.partition('/')
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    package = urllib.parse.quote(name, safe='')
    versions: list[dict[str, Any]] = []
    base = ''
    try:
        for kind in ('orgs', 'users'):
            base = f'{GITHUB_API}/{kind}/{owner}/packages/container/{package}/versions'
            response = client.get(base, params={'per_page': '100'}, headers=headers)
            if response.status_code == 404 and kind == 'orgs':
                continue
            break
        seen: set[str] = set()
        while True:
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise RegistryError(f'{reference}: the API listed no versions')
            versions += page
            following = response.links.get('next', {}).get('url')
            if not following or following in seen:  # a server repeating a page ends the listing
                break
            seen.add(following)
            response = client.get(following, headers=headers)
        listed: dict[int, dict[str, Any]] = {}
        for version in versions:
            tags = version.get('metadata', {}).get('container', {}).get('tags')
            if not isinstance(tags, list):
                raise RegistryError(f'{reference}: version {version.get("id")} lists no tags')
            listed[int(version['id'])] = version
        untagged = [v for v in listed.values() if not v['metadata']['container']['tags']]
        untagged.sort(key=lambda v: str(v['created_at']), reverse=True)
        removed = []
        for version in untagged[keep_untagged:]:
            client.delete(f'{base}/{version["id"]}', headers=headers).raise_for_status()
            removed.append(int(version['id']))
    except httpx.HTTPError as error:
        raise RegistryError(f'{reference}: {_reason(error)}') from error
    return removed
