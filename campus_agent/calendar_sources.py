"""Compatibility import; implementation migrated to connectors.campus.calendar_sources."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connectors.campus.calendar_sources')
