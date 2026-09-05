# sqlitexplorer

A command-line explorer for SQLite databases. It lists tables, prints schemas,
dumps rows, computes statistics, searches values, runs ad-hoc queries, draws
charts, exports and imports data, and offers an interactive shell. Tables are
rendered with [outfancy](https://github.com/carlosplanchon/outfancy) and charts
with [plotille](https://github.com/tammoippen/plotille).

## Installation

Requires Python 3.10 or newer.

```sh
# From a clone of this repository:
uv tool install .
# or with pip:
pip install .
```

## Quick tour

```sh
sqlitexplorer tables app.db                     # tables and views with their row counts
sqlitexplorer schema app.db [users]             # CREATE statements, in creation order
sqlitexplorer describe app.db users             # columns: type, NOT NULL, default, primary key
sqlitexplorer indexes app.db [users]            # indexes and the columns they cover
sqlitexplorer foreign-keys app.db [posts]       # foreign keys
sqlitexplorer info app.db [--check]             # size, pragmas, object counts, integrity check
sqlitexplorer show app.db users                 # rows of a table or view
sqlitexplorer stats app.db users                # nulls, distinct, min, max, top values per column
sqlitexplorer search app.db "marie"             # find a text in every column of every table
sqlitexplorer query app.db "SELECT ..."         # run SQL
sqlitexplorer chart app.db "SELECT day, total FROM sales ORDER BY day"
sqlitexplorer dump app.db                       # the whole database as SQL
sqlitexplorer export app.db users -o users.csv  # rows to a file
sqlitexplorer import app.db people people.csv   # a file into a table
sqlitexplorer diff app.db backup.db             # compare two schemas
sqlitexplorer shell app.db                      # interactive shell
```

Every command has `--help`. Databases are opened read-only and are never
created; commands that change data need `--write`, except `import`, which
always writes.

## Exploring

`show` accepts filters so most questions need no SQL:

```sh
sqlitexplorer show app.db users -c name,age --where "age > 18" --order-by age --desc -n 20 --offset 40
```

`--columns` and `--order-by` are validated against the table; `--where` is a
raw SQL condition.

`stats` reports, for each column, the declared type, how many NULLs, how many
distinct values, the minimum, the maximum and the most frequent values
(`--top N`). `search` looks for a case-insensitive substring in every non-BLOB
column of every table (`--table` restricts it, `--limit` stops early) and
prints the table, column, rowid and value of each match.

## Output options

Every command that prints rows takes the same options:

| Option | Effect |
|---|---|
| `--format`, `-f` | `table` (default), `csv`, `tsv`, `json` or `markdown` |
| `--null TEXT` | text shown for NULL values (default `NULL`) |
| `--truncate N` | cut values longer than N characters |
| `--page N`, `--page-size M` | print only one page of the rows; the footer goes to stderr |
| `--pager` | send the output to `$PAGER` (default `less -R`) when on a terminal |
| `--color` / `--no-color` | force or disable ANSI colors; by default only on a terminal, never when `NO_COLOR` is set |
| `--width N` | width to fit tables into (default: the terminal width) |

`NULL` and `BLOB` values are printed as SQL literals (`NULL`, `X'0102'`). In
the JSON format NULL becomes `null`, BLOBs are base64 strings and values are
never truncated.

## Queries

```sh
sqlitexplorer query app.db "SELECT name, age FROM users WHERE age > :min" -p min=30
sqlitexplorer query app.db --file report.sql            # several statements, one transaction
echo "SELECT COUNT(*) FROM users" | sqlitexplorer query app.db -
sqlitexplorer query app.db "SELECT ..." --attach old=backup.db   # then use old.users
sqlitexplorer query app.db "SELECT ..." --explain        # EXPLAIN QUERY PLAN as a tree
sqlitexplorer query app.db "SELECT ..." --time           # rows and elapsed time on stderr
sqlitexplorer query app.db "SELECT COUNT(*) FROM jobs" --watch 2   # re-run every 2 s, Ctrl-C stops
sqlitexplorer query app.db "DELETE FROM users WHERE age IS NULL" --write
```

Several statements can be given at once (separated by `;`); they run in one
transaction that is committed at the end and rolled back on the first error.
`--param` values that look like numbers are bound as numbers. Attached
databases follow the read-only rule of the main one.

## Charts

```sh
sqlitexplorer chart app.db "SELECT day, sales, returns FROM daily ORDER BY day"
sqlitexplorer chart app.db "SELECT age FROM users" --kind hist --bins 20
```

The first column is the X axis (numbers or ISO dates), every other column is a
series named after the column; rows with NULLs are skipped and counted on
stderr. `--kind` selects `line` (default), `scatter` or `hist` (first column
only, `--bins`). `--height`, `--width`, `--x-label`, `--y-label` and `--color`
adjust the drawing.

## Export, import, dump and diff

```sh
sqlitexplorer dump app.db -o backup.sql                  # replayable SQL, like .dump
sqlitexplorer export app.db users -f json -o users.json
sqlitexplorer export app.db --all -o exported/ -f csv    # one file per table and view
sqlitexplorer import app.db people people.csv            # csv, tsv or json (list of objects)
sqlitexplorer diff app.db backup.db                      # exit status 1 when the schemas differ
```

`import` creates the table when it does not exist, inferring INTEGER, REAL or
TEXT for each column; empty CSV cells become NULL. The format comes from the
file extension unless `--format` is given.

## Shell

```
$ sqlitexplorer shell app.db
sqlitexplorer> SELECT name
        ...> FROM users;
sqlitexplorer> .tables
sqlitexplorer> .format json
sqlitexplorer> .quit
```

Statements end with `;` and may span several lines. Dot-commands: `.tables`,
`.schema [NAME]`, `.describe TABLE`, `.indexes [TABLE]`, `.stats TABLE`,
`.format FORMAT`, `.null TEXT`, `.truncate N|off`, `.help` and `.quit`. Tab
completes SQL keywords, table and column names; the history is kept in
`$XDG_STATE_HOME/sqlitexplorer/history` (`~/.local/state` by default). With
`--write` every statement is committed as soon as it succeeds. SQL can also be
piped into the shell.

Completion of the command line itself (commands, options and table names) is
installed with `sqlitexplorer --install-completion`.

## Development

```sh
uv venv
uv pip install -e ".[dev]"
uv run pytest
uv run ruff check .
```

## Releasing

Releases are driven by version tags. Pushing `vX.Y.Z` runs the tests, builds
the distributions, publishes them to PyPI with
[trusted publishing](https://docs.pypi.org/trusted-publishers/) and creates a
GitHub release with the artifacts attached.

```sh
uv version 0.3.0                      # or: uv version --bump minor
git commit -am "Release 0.3.0"
git tag v0.3.0
git push origin master v0.3.0
```

The tag must match the version in `pyproject.toml`; the workflow refuses to
publish otherwise. Before the first release, register the repository as a
trusted publisher of the project on PyPI with the workflow name `release.yml`
and the environment `pypi`, and create that environment in the repository
settings on GitHub.

## License

MIT. See [LICENSE](LICENSE).
