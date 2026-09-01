"""Password gate simple para el dashboard.

El hash bcrypt vive en ``src/app/dashboard/auth_config.yaml`` (git-ignored).
Se genera con ``python -m app.dashboard.auth set-password``.
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
import streamlit as st
import yaml


_CONFIG_PATH = Path(__file__).parent / "auth_config.yaml"


def _load_hash() -> bytes | None:
    if not _CONFIG_PATH.exists():
        return None
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    h = data.get("password_hash")
    return h.encode("utf-8") if h else None


def _save_hash(password: str) -> None:
    salt = bcrypt.gensalt(rounds=12)
    h = bcrypt.hashpw(password.encode("utf-8"), salt)
    _CONFIG_PATH.write_text(
        yaml.safe_dump({"password_hash": h.decode("utf-8")}),
        encoding="utf-8",
    )


def require_password() -> None:
    """Bloquea la app hasta que el usuario ingrese la contraseña correcta.

    Si no hay hash configurado, muestra un mensaje pidiendo correr el setup.
    """
    stored_hash = _load_hash()
    if stored_hash is None:
        st.error(
            "Dashboard sin contraseña configurada. Correr desde terminal:\n\n"
            "```\npython -m app.dashboard.auth set-password\n```"
        )
        st.stop()

    if st.session_state.get("_authed"):
        return

    st.title("🔒 Dashboard Maryun")
    pwd = st.text_input("Contraseña", type="password", key="_pwd_input")
    if st.button("Ingresar", type="primary") or pwd:
        if pwd and bcrypt.checkpw(pwd.encode("utf-8"), stored_hash):
            st.session_state["_authed"] = True
            st.rerun()
        elif pwd:
            st.error("Contraseña incorrecta.")
    st.stop()


def _cli_set_password() -> None:
    """Ejecutar: ``python -m app.dashboard.auth set-password``."""
    import getpass

    pwd1 = getpass.getpass("Nueva contraseña: ")
    pwd2 = getpass.getpass("Repetir: ")
    if pwd1 != pwd2:
        print("No coinciden.")
        raise SystemExit(1)
    if len(pwd1) < 8:
        print("Mínimo 8 caracteres.")
        raise SystemExit(1)
    _save_hash(pwd1)
    print(f"Hash guardado en {_CONFIG_PATH}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "set-password":
        _cli_set_password()
    else:
        print("Uso: python -m app.dashboard.auth set-password")
