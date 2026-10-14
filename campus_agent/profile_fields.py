"""Compatibility import; implementation migrated to connectors.campus.profile_fields."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connectors.campus.profile_fields')
