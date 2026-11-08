# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""aptly, in its real image, reads what the Signer signs, whichever user it runs as."""

import json
from pathlib import Path

import pytest

from distropack_builder.bake import REGISTRY, tool_image
from distropack_builder.builders import APTLY_CONFIG
from distropack_builder.docker import Engine
from distropack_builder.signing import Signer
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

pytestmark = [pytest.mark.e2e, pytest.mark.enable_socket]

DSC = b"""Format: 3.0 (quilt)
Source: hello
Binary: hello
Architecture: all
Version: 1.0-1
Maintainer: distropack test <test@example.invalid>
Checksums-Sha256:
 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 0 hello_1.0.orig.tar.gz
Files:
 d41d8cd98f00b204e9800998ecf8427e 0 hello_1.0.orig.tar.gz
"""


def test_adds_a_signed_dsc_as_a_user_without_home(docker_engine: None, tmp_path: Path) -> None:
    """Under rootful Docker, aptly runs as the invoking user, who has no home in the image.

    The uid is one no image knows; aptly keeps its database in the container, since a subordinate
    uid of a rootless engine cannot write the mounts.
    """
    data = tmp_path / 'data'
    data.mkdir()
    signer = Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)
    (data / 'hello_1.0-1.dsc').write_bytes(signer.clearsign(DSC))
    (data / 'hello_1.0.orig.tar.gz').write_bytes(b'')
    config = json.dumps({**APTLY_CONFIG, 'rootDir': '/tmp/aptly'})
    for path in (tmp_path, data):
        path.chmod(0o755)
    for path in data.iterdir():
        path.chmod(0o644)
    script = (
        f"printf '%s' '{config}' > /tmp/aptly.conf"
        ' && aptly -config=/tmp/aptly.conf repo create hello'
        ' && aptly -config=/tmp/aptly.conf repo add hello /data'
    )
    output = Engine.connect().client.containers.run(
        REGISTRY + tool_image('aptly'),
        ['sh', '-c', script],
        user='4242:4242',
        volumes={str(data): {'bind': '/data', 'mode': 'ro'}},
        remove=True,
        stderr=True,
    )
    assert b'[+] hello_1.0-1_source added' in output, output.decode()
