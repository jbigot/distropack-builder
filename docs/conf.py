"""Sphinx configuration of the documentation of distro-releases and distropack-builder."""

project = 'distropack-builder'
extensions = ['myst_parser', 'sphinx.ext.autodoc', 'sphinx.ext.napoleon']
html_theme = 'furo'
napoleon_google_docstring = True
