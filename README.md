# sqlitexplorer

A small command-line explorer for SQLite databases. It lists tables, prints
schemas, dumps rows and runs ad-hoc queries, rendering the results as terminal
tables with [outfancy](https://github.com/carlosplanchon/outfancy).

## Installation

Requires Python 3.10 or newer.

```sh
# From a clone of this repository:
uv tool install .
# or with pip:
pip install .
```

## Usage

```sh
sqlitexplorer tables app.db                 # tables and views with their row counts
sqlitexplorer schema app.db                 # every CREATE statement in the database
sqlitexplorer schema app.db users           # one table with its indexes and triggers
sqlitexplorer describe app.db users         # columns: type, NOT NULL, default, primary key
sqlitexplorer show app.db users             # all the rows of a table or view
sqlitexplorer show app.db users -n 20 --offset 40
sqlitexplorer query app.db "SELECT name, age FROM users WHERE age > 30"
echo "SELECT COUNT(*) FROM users" | sqlitexplorer query app.db -
sqlitexplorer query app.db "DELETE FROM users WHERE age IS NULL" --write
```

Databases are opened read-only and are never created. Pass `--write` to
`query` to run statements that modify data; the change is committed when the
statement succeeds and rolled back otherwise. `NULL` and `BLOB` values are
printed as SQL literals (`NULL`, `X'0102'`).

Tables adapt to the terminal width. Use `--width N` to render for another width
(for example when redirecting the output to a file) and `--color` /
`--no-color` to override the automatic color detection (`NO_COLOR` is
honoured).

Run `sqlitexplorer --help` or `sqlitexplorer <command> --help` for every option.

## Development

```sh
uv venv
uv pip install -e ".[dev]"
uv run pytest
uv run ruff check .
```
