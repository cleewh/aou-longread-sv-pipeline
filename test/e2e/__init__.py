"""Test/e2e harness package.

Makes :mod:`cost_regression` importable as ``test.e2e.cost_regression`` when
the repo root is on ``sys.path``. The property tests also patch
``sys.path`` directly to import the module by bare name.
"""
