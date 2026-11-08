# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Sphinx configuration of the documentation of distro-releases and distropack-builder."""

import json
from pathlib import Path

from sphinx_polyversion.api import LoadError, load

from distropack_builder import config

# The reference of build.conf renders its JSON Schema, generated from the models before each build.
_GENERATED = Path(__file__).parent / 'reference' / '_generated'
_GENERATED.mkdir(exist_ok=True)
(_GENERATED / 'build-conf.schema.json').write_text(json.dumps(config.schema(), indent=2) + '\n')

# Under sphinx-polyversion, the versions of the site and the commit this one is built from; a
# plain sphinx-build builds main's working tree.
try:
    _polyversion = load(globals())
except LoadError:
    _polyversion = {}
_commit = _polyversion.get('commit', 'main')

project = 'distropack-builder'
extensions = [
    'myst_parser',
    'sphinx.ext.autodoc',
    'sphinx.ext.extlinks',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinx-jsonschema',
    'sphinxcontrib.typer',
]
intersphinx_mapping = {'python': ('https://docs.python.org/3', None)}
# Docstrings write code between single backticks, as Markdown does.
default_role = 'code'
html_theme = 'furo'
templates_path = ['_templates']
# Furo's default sidebar (its theme.conf), with the versions after the search box.
html_sidebars = {
    '**': [
        'sidebar/brand.html',
        'sidebar/search.html',
        'sidebar/versions.html',
        'sidebar/scroll-start.html',
        'sidebar/navigation.html',
        'sidebar/ethical-ads.html',
        'sidebar/scroll-end.html',
        'sidebar/variant-selector.html',
    ]
}
# {source}`path` links a file of the repository, at the commit the documentation is built from.
extlinks = {'source': (f'https://github.com/jbigot/distropack-builder/blob/{_commit}/%s', '%s')}
napoleon_google_docstring = True
# httpx documents itself with MkDocs, which publishes no inventory to link to.
nitpick_ignore = [('py:class', 'httpx.Client')]
