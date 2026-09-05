"""Shell-completion callbacks for Typer parameters."""

from __future__ import annotations

from pathlib import Path

import typer

from sqlitexplorer.core import ExplorerError, open_database


def complete_table(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a table or view name from the database given earlier on the command line."""
    database = ctx.params.get("database")
    if not database:
        return []
    try:
        with open_database(Path(database)) as db:
            names = db.names()
    except (ExplorerError, OSError):
        return []
    return [name for name in names if name.startswith(incomplete)]
