# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Serve recorded release data to the builder when the tests run it as a subprocess.

Python imports this module at startup when its directory is on PYTHONPATH, before the builder makes
any request. Recorded URLs (DISTROPACK_TEST_RECORDINGS/urls.json) are served from their recording,
the local test server (127.0.0.1) is reached for real, the registry publishes every builder image
(the tests bake those they run), and any other URL fails, naming it: the
builder runs unchanged, with neither network nor date-dependent data. The builder fetches everything
with httpx, served by respx.
"""

import json
import os
from pathlib import Path

_RECORDINGS = Path(os.environ['DISTROPACK_TEST_RECORDINGS'])
_URLS: dict[str, str] = json.loads((_RECORDINGS / 'urls.json').read_text())

try:
    import httpx
    import respx
except ImportError:
    # The interpreters without them (the installed wheels of the packaging tests) fetch nothing.
    pass
else:
    _router = respx.mock(assert_all_called=False, assert_all_mocked=True)
    for _url, _name in _URLS.items():
        _router.get(_url).mock(
            return_value=httpx.Response(200, content=(_RECORDINGS / _name).read_bytes())
        )
    _router.route(host='127.0.0.1').pass_through()
    # the builder images the tests run are baked into the engine beforehand: published
    _router.head(url__regex=r'^https://ghcr\.io/v2/.*/manifests/').respond(200)
    _router.start()
