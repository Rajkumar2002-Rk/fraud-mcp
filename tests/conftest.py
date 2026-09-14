"""Shared fixtures.

Every test runs against a freshly seeded database in a temp directory, pointed at
via FRAUD_MCP_DB, so the committed dataset is never mutated by `flag_case`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fraud_mcp import db as dbmod
from fraud_mcp.seed import build_database


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("fraud-db") / "test.sqlite3"
    build_database(path)
    return path


@pytest.fixture(autouse=True)
def _point_at_test_db(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAUD_MCP_DB", str(seeded_db))


@pytest.fixture
def conn(seeded_db: Path):
    c = dbmod.connect(seeded_db)
    yield c
    c.close()


@pytest.fixture
def now(conn):
    return dbmod.as_of(conn)
