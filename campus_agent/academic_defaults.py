"""Compatibility import; implementation migrated to connectors.campus.academic_defaults."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connectors.campus.academic_defaults')
