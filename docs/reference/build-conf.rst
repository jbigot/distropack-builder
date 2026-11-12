.. SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
..
.. SPDX-License-Identifier: MIT

..
   reStructuredText rather than MyST: inside MyST's eval-rst block, sphinx-jsonschema's
   references to its own definitions do not resolve.

The build configuration, ``build.conf``
=======================================

``build.conf`` is a YAML file. Every path in it is relative to its own directory, not to the
directory the builder runs from. A top-level key that starts with a dot is ignored: it can hold
YAML anchors for the distributions to merge, as in

.. code-block:: yaml

   .distribution: &distribution
     repository: {name: example, description: Example packages}
   distribs:
     debian:
       <<: *distribution
       versions: [supported, testing]
   packages:
     example: {}

Any other key the reference below does not list is an error, as is a value of the wrong type: the
builder stops before any work with one line naming the file, the setting
(``distribs.debian.repository.urll``) and the reason. ``distropack-build config schema`` prints the
JSON Schema of the file, for an editor to check it as you type.

The directory of each package, beside ``build.conf``, holds ``debian/``, ``<name>.spec``, or both:
a distribution of the Debian family builds the packages with ``debian/``, one of the Fedora family
those with a spec.

The repository pages
--------------------

A repository with a ``url`` gets an ``index.html`` page saying how to install from it, release by
release. The page is the Jinja template ``README.tpl/README.html.j2`` beside ``build.conf`` when
there is one, the builder's default one otherwise. Its one variable, ``page``, holds the repository
(``distribution_id``, ``distribution_name``, ``family``, ``repository_name``, ``description``,
``url``, ``key_url``) and its ``releases``, each with its ``label``, ``codename``, ``suite``,
``version``, ``url`` and, for rpm, ``repo_file``, the URL of its ``.repo`` file. A project's
template can extend the default one and replace some of its blocks, ``title``, ``intro``,
``install``, or ``release``, the instructions for one release:

.. code-block:: jinja

   {% extends "distropack/README.html.j2" %}
   {% block intro %}<p>The packages of Example, rebuilt every night.</p>{% endblock %}

A template that does not compile, or that names a variable ``page`` does not have, stops the run
before any build.

``distropack-build site`` writes the site's own index page from ``README.tpl/index.html.j2`` when
the project has one, the default otherwise; it may extend ``distropack/index.html.j2`` and override
its blocks ``title``, ``intro`` and ``repositories``. Its one variable, ``site``, holds the
``title`` (the project directory's name) and the ``repositories``, each with its ``label`` and its
``path`` in the site.

Settings
--------

.. jsonschema:: _generated/build-conf.schema.json
   :lift_definitions:
   :auto_reference:
   :auto_target:
