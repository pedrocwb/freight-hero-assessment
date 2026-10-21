"""Which database the Postgres tests run on.

`TEST_DATABASE_URL`, else `DATABASE_URL` (as `make integration` sets it). Without either
every Postgres test skips, so `make test` and `make ci` need no database. Each test works
in a schema of its own (`migrate.scratch_schema`), dropped afterwards, so the URL may
point at a database that holds data: nothing outside that schema is touched.
"""

import os

import pytest

DATABASE_ENV = ("TEST_DATABASE_URL", "DATABASE_URL")


def database_url_for_tests() -> str | None:
    """The database the Postgres tests run on, or None to skip them."""
    return next((value for name in DATABASE_ENV if (value := os.environ.get(name))), None)


SKIP_REASON = f"set {' or '.join(DATABASE_ENV)} to run the Postgres tests"
DATABASE_URL = database_url_for_tests() or ""

# The marks of a module whose every test needs the database.
POSTGRES = [pytest.mark.integration, pytest.mark.skipif(not DATABASE_URL, reason=SKIP_REASON)]
