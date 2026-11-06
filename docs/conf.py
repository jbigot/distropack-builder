"""Sphinx configuration of the documentation of distro-releases and distropack-builder."""

project = 'distropack-builder'
extensions = [
    'myst_parser',
    'sphinx.ext.autodoc',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinxcontrib.typer',
]
intersphinx_mapping = {'python': ('https://docs.python.org/3', None)}
# Docstrings write code between single backticks, as Markdown does.
default_role = 'code'
html_theme = 'furo'
napoleon_google_docstring = True
# httpx documents itself with MkDocs, which publishes no inventory to link to.
nitpick_ignore = [('py:class', 'httpx.Client')]
