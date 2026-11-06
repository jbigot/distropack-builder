"""Serve recorded release data to the builder when the tests run it as a subprocess.

Python imports this module at startup when its directory is on PYTHONPATH, before distinfo imports
urlopen. Recorded URLs (DISTROPACK_TEST_RECORDINGS/urls.json) are served from their recording, the
local test server (127.0.0.1) is reached for real, and any other URL fails, naming it: the builder
runs unchanged, with neither network nor date-dependent data.
"""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_RECORDINGS = Path(os.environ['DISTROPACK_TEST_RECORDINGS'])
_URLS: dict[str, str] = json.loads((_RECORDINGS / 'urls.json').read_text())
_real_urlopen = urllib.request.urlopen


def _urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
    address = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    if urlparse(address).hostname == '127.0.0.1':
        return _real_urlopen(url, *args, **kwargs)
    if address not in _URLS:
        raise KeyError(f'no recording for {address}')
    return (_RECORDINGS / _URLS[address]).open('rb')


urllib.request.urlopen = _urlopen
