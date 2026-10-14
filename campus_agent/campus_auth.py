"""Compatibility import; implementation migrated to connectors.campus.campus_auth."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connectors.campus.campus_auth')
