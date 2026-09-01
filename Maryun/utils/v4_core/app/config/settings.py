"""Application settings (pydantic-settings).

All environment variables are prefixed with ``MARYUN_`` and use ``__`` for
nesting (so ``MARYUN_MARIADB__HOST`` maps to ``settings.mariadb.host``).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MariaDBSettings(BaseModel):
    host: str = "localhost"
    port: int = 3306
    database: str = "maryun_erp"
    user: str = "mry_repl_ro"
    password: str = ""
    ssl_enabled: bool = False

    def sqlalchemy_url(self) -> str:
        # ``pymysql`` is the default DBAPI driver for MariaDB under SQLAlchemy.
        ssl_suffix = "?ssl=true" if self.ssl_enabled else ""
        return (
            f"mysql+pymysql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}{ssl_suffix}"
        )


class RunSettings(BaseModel):
    run_forecast: bool = True
    run_classification: bool = True
    source: Literal["mariadb", "mock"] = "mock"
    # If empty string, resolved to today() at RunContext creation.
    process_date: str = ""


class PathsSettings(BaseModel):
    data_root: str = "data"
    input_dir: str = "data/input"
    reference_dir: str = "data/reference"
    cache_dir: str = "data/cache"
    output_dir: str = "data/output"


class LogSettings(BaseModel):
    level: str = "INFO"
    json: bool = False


class ExternalSourcesSettings(BaseModel):
    """Fuentes externas de archivos maestros (Excel de OneDrive / red).

    Cuando estas rutas estan seteadas, el pipeline las usa directamente
    en lugar de buscar en data/reference/. Permite mantener una unica
    fuente de verdad sin duplicar archivos.
    """
    # Pronosticos de demanda (ej. OneDrive/PRONOSTICOS_DEMANDA_v10.xlsx)
    forecast_xlsx: str = ""
    # Clasificacion ABC/XYZ externa (ej. OneDrive/RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx)
    abc_xyz_xlsx: str = ""
    # Si True, al inicio del pipeline regenera override_clasificacion.xlsx si v3 cambio
    auto_sync: bool = True


class SupabaseSettings(BaseModel):
    """Conexion a Supabase (backend en la nube del dashboard).

    - El PIPELINE usa ``service_key`` (escritura) para empujar el plan.
    - El DASHBOARD usa ``service_key`` (lee + escribe estado/cantidad/nota).
      Las llaves se inyectan por entorno (.env local / secrets de Streamlit Cloud);
      NUNCA se commitean al repo.
    """
    enabled: bool = False
    url: str = ""             # https://xxxx.supabase.co
    service_key: str = ""     # service_role key (escritura)
    anon_key: str = ""        # anon key (lectura, opcional)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MARYUN_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mariadb: MariaDBSettings = Field(default_factory=MariaDBSettings)
    run: RunSettings = Field(default_factory=RunSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    external: ExternalSourcesSettings = Field(default_factory=ExternalSourcesSettings)
    supabase: SupabaseSettings = Field(default_factory=SupabaseSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Reset cache — tests may override env vars between runs."""
    get_settings.cache_clear()
