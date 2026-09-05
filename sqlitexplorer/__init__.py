"""Explore SQLite databases from the terminal."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sqlitexplorer")
except PackageNotFoundError:  # pragma: no cover - source checkout that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
