"""Project paths — single source of truth for where files live on disk.

Every module that reads/writes files goes through :class:`ProjectPaths` so the
pipeline can be rerouted (e.g. to a temp dir in tests) by tweaking one object.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.config.settings import Settings, get_settings


class ProjectPaths(BaseModel):
    """Resolved filesystem paths for the replenishment pipeline.

    All paths are absolute. Directories are created lazily via
    :meth:`ensure_dirs`.
    """

    project_root: Path
    data_root: Path
    input_dir: Path
    reference_dir: Path
    cache_dir: Path
    output_dir: Path
    config_dir: Path
    # Rutas externas (OneDrive / red). None si no estan configuradas.
    external_forecast_xlsx: Path | None = None
    external_abc_xyz_xlsx: Path | None = None

    # ----- reference (version-controlled, user-edited) --------------------
    def reference_file(self, name: str) -> Path:
        return self.reference_dir / name

    @property
    def homologacion_productos_xlsx(self) -> Path:
        return self.reference_file("homologacion_productos.xlsx")

    @property
    def override_forecast_xlsx(self) -> Path:
        return self.reference_file("override_forecast.xlsx")

    @property
    def override_clasificacion_xlsx(self) -> Path:
        return self.reference_file("override_clasificacion.xlsx")

    @property
    def distancias_xlsx(self) -> Path:
        return self.reference_file("distancias_sucursales.xlsx")

    @property
    def costos_transporte_xlsx(self) -> Path:
        return self.reference_file("costos_transporte.xlsx")

    @property
    def prioridad_cd_xlsx(self) -> Path:
        return self.reference_file("prioridad_cd.xlsx")

    @property
    def proveedores_xlsx(self) -> Path:
        return self.reference_file("proveedores.xlsx")

    @property
    def reglas_sku_sucursal_xlsx(self) -> Path:
        return self.reference_file("reglas_sku_sucursal.xlsx")

    @property
    def automatizacion_xlsx(self) -> Path:
        return self.reference_file("automatizacion_sucursales.xlsx")

    @property
    def forecast_precomputed_xlsx(self) -> Path:
        """Ruta al archivo de pronosticos precomputados.

        Si MARYUN_EXTERNAL__FORECAST_XLSX esta configurado, SIEMPRE devuelve esa
        ruta oficial (sin fallback silencioso a copias locales). El stage de
        validacion (stage_external_sync) ya garantizo que existe y es accesible.
        Solo usa el archivo local cuando NO hay ruta externa configurada.
        """
        if self.external_forecast_xlsx is not None:
            return self.external_forecast_xlsx
        return self.reference_file("forecast_precomputado.xlsx")

    @property
    def classification_precomputed_xlsx(self) -> Path:
        return self.reference_file("clasificacion_precomputada.xlsx")

    @property
    def stock_valorizado_xlsx(self) -> Path:
        """Stock con PMP (Precio Medio Ponderado) por SKU × sucursal."""
        return self.reference_file("stock_valorizado.xlsx")

    # ----- estado persistente (se actualiza en cada run exitoso) ------------
    @property
    def silence_registry_csv(self) -> Path:
        """Registro de ventana de silencio — persiste entre corridas."""
        return self.data_root / "silence_registry.csv"

    # ----- outputs (per-run folder) ---------------------------------------
    def run_output_dir(self, run_id: str) -> Path:
        return self.output_dir / run_id

    def ensure_dirs(self) -> None:
        """Create required directories if they don't yet exist."""
        for p in (
            self.data_root,
            self.input_dir,
            self.reference_dir,
            self.cache_dir,
            self.output_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _resolve(base: Path, candidate: str) -> Path:
    p = Path(candidate)
    return p if p.is_absolute() else (base / p).resolve()


def build_paths(settings: Settings | None = None, project_root: Path | None = None) -> ProjectPaths:
    """Construct :class:`ProjectPaths` from settings + project root."""
    s = settings or get_settings()
    root = project_root or Path.cwd()
    ext_forecast = Path(s.external.forecast_xlsx) if s.external.forecast_xlsx else None
    ext_abc_xyz = Path(s.external.abc_xyz_xlsx) if s.external.abc_xyz_xlsx else None
    return ProjectPaths(
        project_root=root,
        data_root=_resolve(root, s.paths.data_root),
        input_dir=_resolve(root, s.paths.input_dir),
        reference_dir=_resolve(root, s.paths.reference_dir),
        cache_dir=_resolve(root, s.paths.cache_dir),
        output_dir=_resolve(root, s.paths.output_dir),
        config_dir=(root / "src" / "app" / "config").resolve(),
        external_forecast_xlsx=ext_forecast,
        external_abc_xyz_xlsx=ext_abc_xyz,
    )


@lru_cache(maxsize=1)
def get_paths() -> ProjectPaths:
    """Return the cached :class:`ProjectPaths` singleton."""
    return build_paths()


def reset_paths_cache() -> None:
    get_paths.cache_clear()
