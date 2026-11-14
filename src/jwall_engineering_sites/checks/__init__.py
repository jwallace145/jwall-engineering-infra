"""A pluggable preflight check framework; see framework.py."""

from __future__ import annotations

import importlib

_BUILTIN = (
    "databricks_config",
    "databricks_live",
    "databricks_between",
    "databricks_after",
)


def load_builtin_checks() -> None:
    """Import every built-in check module so its `@check` functions register.

    The orchestrator calls this once before running a stage. Importing is
    idempotent: a module registers only the first time it is imported.
    """
    for name in _BUILTIN:
        importlib.import_module(f"{__name__}.{name}")
