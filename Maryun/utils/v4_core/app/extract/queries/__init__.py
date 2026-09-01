"""SQL resources — loaded as plain strings, not imported."""
from importlib.resources import files


def load_sql(name: str) -> str:
    """Return the text of ``queries/{name}.sql``."""
    return (files(__package__) / f"{name}.sql").read_text(encoding="utf-8")


__all__ = ["load_sql"]
