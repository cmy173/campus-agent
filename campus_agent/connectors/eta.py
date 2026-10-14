"""Compatibility import for migrated campus protocols."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connectors.campus.connectors.eta')
