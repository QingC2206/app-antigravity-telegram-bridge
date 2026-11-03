"""Compatibility import; implementation lives in antigravity_telegram_bridge.storage.bridge."""
import importlib as _importlib
import sys as _sys

_implementation = _importlib.import_module('antigravity_telegram_bridge.storage.bridge')
_sys.modules[__name__] = _implementation
