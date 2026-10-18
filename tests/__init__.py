"""Test package for the harness.

Importing any test module loads this package first, so the ``openai`` import
shim is installed before the harness core is imported. The shim only exists
when the real package is missing (bare interpreter); tests never talk to a
real model server — they patch ``core.agent.OpenAI`` with a scripted client.

``OPENAI_AVAILABLE`` tells subprocess-based tests (the real CLI smoke test)
whether the interpreter can import the real package.
"""
from __future__ import annotations

import sys
import types

try:  # pragma: no cover - depends on the environment
    import openai  # noqa: F401
except ImportError:  # bare environment without pip packages
    OPENAI_AVAILABLE = False
    _shim = types.ModuleType("openai")

    class _PlaceholderOpenAI:
        """Never used: tests always patch core.agent.OpenAI before construction."""

        def __init__(self, *args, **kwargs):  # noqa: D107
            raise RuntimeError("openai package is missing; patch core.agent.OpenAI in tests")

    _shim.OpenAI = _PlaceholderOpenAI  # type: ignore[attr-defined]
    sys.modules["openai"] = _shim
else:
    OPENAI_AVAILABLE = True