# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Sphinx configuration of the documentation of distro-releases and distropack-builder."""

import json
import sys
from pathlib import Path

from sphinx_polyversion.api import LoadError, load

from distropack_builder import config

# The reference of build.conf renders its JSON Schema, generated from the models before each build.
_GENERATED = Path(__file__).parent / 'reference' / '_generated'
_GENERATED.mkdir(exist_ok=True)
(_GENERATED / 'build-conf.schema.json').write_text(json.dumps(config.schema(), indent=2) + '\n')

# The reference of the two actions, from their action.yml.
sys.path.insert(0, str(Path(__file__).parent))
from action_reference import action_reference  # noqa: E402

_REPO = Path(__file__).parent.parent
(_GENERATED / 'action.md').write_text(action_reference(_REPO / 'action.yml'))
(_GENERATED / 'plan-action.md').write_text(action_reference(_REPO / 'plan' / 'action.yml'))

# Under sphinx-polyversion, the versions of the site and the git ref this one's links name (its
# tag, or main's commit); a plain sphinx-build builds main's working tree.
try:
    _polyversion = load(globals())
except LoadError:
    _polyversion = {}
_source_ref = _polyversion.get('source_ref', 'main')

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
# The generated pages are included by those of reference/, not documents of their own.
exclude_patterns = ['reference/_generated']
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
# {source}`path` links a file of the repository, as of the version the documentation is built from.
extlinks = {'source': (f'https://github.com/jbigot/distropack-builder/blob/{_source_ref}/%s', '%s')}
napoleon_google_docstring = True
# httpx documents itself with MkDocs, which publishes no inventory to link to.
nitpick_ignore = [('py:class', 'httpx.Client')]
