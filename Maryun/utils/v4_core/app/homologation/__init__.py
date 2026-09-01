"""Homologation package.

Mage V4 imports analytical/operational modules directly. Keep this initializer
side-effect free so importing one submodule does not load Excel readers or
legacy file loaders.
"""

__all__: list[str] = []
