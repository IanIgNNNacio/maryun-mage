"""Connectors package.

Mage V4 imports concrete connectors directly only when a legacy file/database
loader is explicitly used. Avoid eager imports here so ClickHouse-based blocks
do not pull Excel or MariaDB dependencies by side effect.
"""

__all__: list[str] = []
