"""Run storage-level tests on both backends: SQLite (a temp file) and Postgres (embedded server
from the `pgserver` package, one fresh database per test)."""

import uuid

import pytest


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield srv
    srv.cleanup()


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "board.db")
    srv = request.getfixturevalue("pg_server")
    name = "t_" + uuid.uuid4().hex[:12]
    srv.psql(f"CREATE DATABASE {name};")
    return srv.get_uri().replace("/postgres?", f"/{name}?", 1)


@pytest.fixture
def store(db_url):
    from huddlehut.store import Store

    return Store(db_url)


@pytest.fixture(autouse=True)
def _no_codex_wake(monkeypatch):
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "0")  # never resume a real Codex thread from a test
