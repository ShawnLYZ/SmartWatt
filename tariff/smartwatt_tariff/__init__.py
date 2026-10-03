"""Sarawak Energy Tariff D pricing and grid carbon conversion.

Pure library. Zero I/O beyond reading its own configuration.

Note: ``selftest.run_selftest()`` is deliberately not called at import
time, so that a broken configuration does not make this package
unimportable - including for the tests that exist to diagnose it. The
server calls it at boot instead.
"""

__all__ = []
