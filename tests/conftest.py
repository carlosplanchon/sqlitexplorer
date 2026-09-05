"""Shared fixtures: sample databases and a CliRunner wrapper."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from sqlitexplorer.cli import app

runner = CliRunner()

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER,
    avatar BLOB
);
CREATE INDEX users_name_idx ON users (name);
CREATE VIEW adults AS SELECT name, age FROM users WHERE age >= 18;
CREATE TABLE empty (x TEXT);
INSERT INTO users (name, age, avatar) VALUES ('Marie', 30, X'0102');
INSERT INTO users (name, age, avatar) VALUES ('Joseph', NULL, NULL);
INSERT INTO users (name, age, avatar) VALUES ('Ana', 12, NULL);
"""

RELATIONAL_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT
);
CREATE INDEX posts_user_idx ON posts (user_id);
CREATE TABLE notes (code TEXT PRIMARY KEY, body TEXT) WITHOUT ROWID;
INSERT INTO users VALUES (1, 'Marie'), (2, 'Joseph');
INSERT INTO posts VALUES (1, 1, 'Hello world'), (2, 2, 'Second post');
INSERT INTO notes VALUES ('a', '100% done'), ('b', 'a_b test'), ('c', 'plain'),
    ('d', 'axb decoy'), ('e', '100 percent decoy');
"""


def make_database(path: Path, script: str) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(script)
    finally:
        connection.close()
    return path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return make_database(tmp_path / "test.db", SCHEMA)


@pytest.fixture
def relational_database(tmp_path: Path) -> Path:
    return make_database(tmp_path / "relational.db", RELATIONAL_SCHEMA)


@pytest.fixture
def invoke() -> Callable[..., Result]:
    def _invoke(*args: object, **kwargs: object) -> Result:
        return runner.invoke(app, [str(arg) for arg in args], **kwargs)

    return _invoke


@pytest.fixture
def count_users() -> Callable[[Path], int]:
    def _count(database: Path) -> int:
        connection = sqlite3.connect(database)
        try:
            return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            connection.close()

    return _count
