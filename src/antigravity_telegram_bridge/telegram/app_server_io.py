"""Compatibility import; implementation lives in antigravity_telegram_bridge.telegram.delivery."""
import importlib as _importlib
import sys as _sys

_implementation = _importlib.import_module('antigravity_telegram_bridge.telegram.delivery')
_sys.modules[__name__] = _implementation
