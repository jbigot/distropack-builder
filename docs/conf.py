# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Sphinx configuration of the documentation of distro-releases and distropack-builder."""

import json
from pathlib import Path

from distropack_builder import config

# The reference of build.conf renders its JSON Schema, generated from the models before each build.
_GENERATED = Path(__file__).parent / 'reference' / '_generated'
_GENERATED.mkdir(exist_ok=True)
(_GENERATED / 'build-conf.schema.json').write_text(json.dumps(config.schema(), indent=2) + '\n')

project = 'distropack-builder'
extensions = [
    'myst_parser',
    'sphinx.ext.autodoc',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinx-jsonschema',
    'sphinxcontrib.typer',
]
intersphinx_mapping = {'python': ('https://docs.python.org/3', None)}
# Docstrings write code between single backticks, as Markdown does.
default_role = 'code'
html_theme = 'furo'
napoleon_google_docstring = True
# httpx documents itself with MkDocs, which publishes no inventory to link to.
nitpick_ignore = [('py:class', 'httpx.Client')]
