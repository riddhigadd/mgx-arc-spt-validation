"""MGX ARC health-console backend package.

Foundation types, YAML config, transport providers, SQLite persistence, and
background jobs live here. Legacy Flask helpers in ``app.py`` remain the
source of truth for existing ``/api/bmc/*`` routes until a later integration
step registers ``/api/arc/*``.
"""

__all__ = [
    "config",
    "jobs",
    "models",
    "store",
]
