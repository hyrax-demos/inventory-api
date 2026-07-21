"""Conftest: stub out psycopg2 so tests run without a live DB driver."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Provide a lightweight psycopg2 stub so app modules can be imported without
# the binary extension installed in the test environment.
# ---------------------------------------------------------------------------
if "psycopg2" not in sys.modules:
    psycopg2_stub = ModuleType("psycopg2")
    psycopg2_stub.connect = MagicMock()  # type: ignore[attr-defined]

    # psycopg2.extras
    extras_stub = ModuleType("psycopg2.extras")
    extras_stub.RealDictCursor = MagicMock()  # type: ignore[attr-defined]
    psycopg2_stub.extras = extras_stub  # type: ignore[attr-defined]

    # psycopg2.OperationalError  (used in tests)
    psycopg2_stub.OperationalError = type("OperationalError", (Exception,), {})  # type: ignore[attr-defined]

    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = extras_stub
