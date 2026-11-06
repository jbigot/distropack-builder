"""Record the release data distro_releases.distinfo fetches, for the characterization tests.

Run by hand, with the network: it is never collected by pytest. The URLs must stay byte-identical
to the ones distinfo builds, since the recorded urlopen looks them up verbatim.
"""

import json
from pathlib import Path
from urllib.request import urlopen

DATA = Path(__file__).parent / 'data'
BODHI = 'https://bodhi.fedoraproject.org/releases/?exclude_archived=False&rows_per_page=100&page='


def fetch(url: str, name: str, urls: dict[str, str]) -> bytes:
    with urlopen(url) as response:
        content: bytes = response.read()
    (DATA / name).write_bytes(content)
    urls[url] = name
    return content


def main() -> None:
    DATA.mkdir(exist_ok=True)
    urls: dict[str, str] = {}
    for dist in ('debian', 'ubuntu'):
        fetch(f'https://debian.pages.debian.net/distro-info-data/{dist}.csv', f'{dist}.csv', urls)
    page, pages = 1, 1
    while page <= pages:
        pages = json.loads(fetch(BODHI + str(page), f'bodhi-releases-{page}.json', urls))['pages']
        page += 1
    for product in ('almalinux', 'rhel', 'rocky-linux'):
        fetch(
            f'https://endoflife.date/api/v1/products/{product}', f'endoflife-{product}.json', urls
        )
    (DATA / 'urls.json').write_text(json.dumps(urls, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
