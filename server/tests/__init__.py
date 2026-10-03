"""Marks this directory as a package.

Without this, pytest imports ``conftest.py`` and ``support.py`` here as
bare top-level modules. ``contract/tests/conftest.py`` already occupies
the name ``conftest`` on ``sys.path``; two same-named modules collide in
``sys.modules`` and whichever loads last silently wins for every
``from conftest import ...`` in the whole suite. Qualifying these modules
as ``tests.conftest`` / ``tests.support`` avoids the collision entirely.
"""
