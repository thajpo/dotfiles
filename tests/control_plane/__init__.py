"""Disposable fixtures and deterministic adapters for the control-plane phases.

The package intentionally has no production imports.  It is safe to import from
unit tests while the controller itself is still unimplemented.
"""

__all__ = [
    "fake_adapters",
    "fake_clock",
    "helpers",
]
