#!/usr/bin/env python3
"""
GDI Sync System — copia local de los datos de tu municipio.

Copyright (C) 2026 Tecnologia Acuario SAS / Santiago Aranguren
Licenciado bajo AGPL-3.0 — ver LICENSE. Se distribuye SIN NINGUNA GARANTIA.

Descarga los datos del municipio desde el Gateway GDI y los guarda en SQLite local.

Configuracion (.env o variables de entorno):
  GDI_API_KEY=bk-gdi-sync-...
  GDI_GATEWAY_URL=https://xxx-gateway.gdilatam.com

Uso:
  python sync.py            # sync incremental (solo lo que cambio)
  python sync.py --full     # descarga todo desde cero
  python sync.py --pdfs     # incluye descarga de PDFs firmados
"""
__version__ = "3.2.0"

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

def load_config():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

    api_key = os.environ.get("GDI_API_KEY", "").strip()
    gateway_url = os.environ.get("GDI_GATEWAY_URL", "").strip().rstrip("/")
    db_path = os.environ.get("GDI_DB_PATH", "backup.db")
    pdf_dir = os.environ.get("GDI_PDF_DIR", "pdfs")

    if not api_key:
        print("ERROR: GDI_API_KEY no configurado. Copiar config.example.env a .env y completar.")
        sys.exit(1)
    if not gateway_url:
        print("ERROR: GDI_GATEWAY_URL no configurado. Copiar config.example.env a .env y completar.")
        sys.exit(1)

    return {"api_key": api_key, "gateway_url": gateway_url, "db_path": db_path, "pdf_dir": pdf_dir}


# ---------------------------------------------------------------------------
# Actualizaciones
# ---------------------------------------------------------------------------

def _git(repo_dir, *args):
    """Corre un comando git en el repo y devuelve stdout, o None si falla."""
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        check=True, capture_output=True, timeout=15, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout


def check_new_version():
    """Avisa si hay una version nueva publicada, sin tocar nada.

    Nunca interrumpe el sync: si no hay git, no es un clon, o no hay red, se salta
    el chequeo en silencio. No modifica nada (el update lo decide el cliente con
    `git pull`).

    Mira la rama remota a la que sigue tu copia (upstream). Si no hay upstream
    configurado prueba origin/main y despues origin/prd, que son las dos formas en
    las que se distribuye el cliente.
    """
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    candidatas = []
    try:
        upstream = _git(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").strip()
        if upstream:
            candidatas.append(upstream)
    except Exception:
        pass
    candidatas += [c for c in ("origin/main", "origin/prd") if c not in candidatas]

    remote = None
    for ref in candidatas:
        origen, _, rama = ref.partition("/")
        try:
            _git(repo_dir, "fetch", "--quiet", origen, rama)
            remote = _git(repo_dir, "show", f"{ref}:sync.py")
            break
        except Exception:
            continue
    if remote is None:
        return

    m = re.search(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', remote, re.M)
    if not m:
        return
    remote_ver = tuple(int(x) for x in m.groups())
    local_ver = tuple(int(x) for x in __version__.split("."))
    if remote_ver > local_ver:
        print(f"  AVISO   : hay una version nueva ({'.'.join(map(str, remote_ver))}, "
              f"esta es {__version__}). Actualizar con: git pull")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def api_get(url, api_key, timeout=30):
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} en {url}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Error de conexion en {url}: {e.reason}")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

_LEGACY_SUFFIX = "__legacy_nopk"


def init_db(db_path):
    # isolation_level=None: conexion en autocommit "puro" — cada statement se
    # confirma solo si nosotros emitimos BEGIN/COMMIT/ROLLBACK explicitos (ver
    # _ensure_schema y sync_table). Es lo que permite que el DDL (CREATE/ALTER/
    # RENAME/DROP) sea transaccional de verdad: con el isolation_level por
    # defecto de sqlite3, el modulo hace un COMMIT implicito antes de cualquier
    # DDL y lo corre en autocommit SIEMPRE, aunque uno llame a conn.rollback()
    # despues (bug real detectado en revision: C10 del crack de PLAN-5).
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _sync_meta (
            table_name    TEXT PRIMARY KEY,
            last_synced_at TEXT,
            total_rows    INTEGER DEFAULT 0
        )
    """)
    return conn


def _create_table_with_pk(conn, table_name, columns):
    """Crea la tabla con PRIMARY KEY en "id" (la trae el Gateway) si esa columna
    esta presente; si no, crea la tabla sin PK (comportamiento previo)."""
    defs = []
    for col in columns:
        if col == "id":
            defs.append('"id" TEXT PRIMARY KEY')
        else:
            defs.append(f'"{col}" TEXT')
    conn.execute(f'CREATE TABLE "{table_name}" ({", ".join(defs)})')


def _add_missing_columns(conn, table_name, columns):
    """Agrega a table_name las columnas de `columns` que todavia no tenga (TEXT)."""
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')}
    for col in columns:
        if col not in existing:
            conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')


def _finish_legacy_migration(conn, legacy_name, table_name):
    """Paso final compartido por la migracion legacy y por la recuperacion de
    huerfanas (ver mas abajo): crea table_name con PK si todavia no existe (o le
    agrega las columnas que le falten si ya existia), reinserta desde legacy_name
    en orden de rowid (el duplicado mas reciente gana) y borra legacy_name.

    No maneja su propia transaccion: el caller controla BEGIN/COMMIT/ROLLBACK,
    para que RENAME + este paso completo sean una unica unidad atomica.
    """
    cols_info = conn.execute(f'PRAGMA table_info("{legacy_name}")').fetchall()
    col_names_list = [c[1] for c in cols_info]

    dest_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None

    if not dest_exists:
        _create_table_with_pk(conn, table_name, col_names_list)
    else:
        _add_missing_columns(conn, table_name, col_names_list)

    col_names = ", ".join(f'"{c}"' for c in col_names_list)
    conn.execute(
        f'INSERT OR REPLACE INTO "{table_name}" ({col_names}) '
        f'SELECT {col_names} FROM "{legacy_name}" ORDER BY rowid'
    )
    conn.execute(f'DROP TABLE "{legacy_name}"')


def _migrate_legacy_table_if_needed(conn, table_name, sample_row):
    """Compat con BDs creadas antes de esta version: esas tablas no tenian
    PRIMARY KEY, por lo que "INSERT OR REPLACE" nunca reemplazaba y acumulaba
    duplicados en `--full` repetidos. Si detectamos una tabla existente sin PK,
    la migramos en el momento: la renombramos y completamos con
    `_finish_legacy_migration`. No requiere que el usuario borre el .db a mano.

    Se llama SIEMPRE dentro de la transaccion explicita de `_ensure_schema`:
    RENAME + CREATE/ALTER + INSERT + DROP quedan como una unica operacion
    atomica (si el proceso muere a mitad de camino, SQLite revierte todo al
    reabrir la conexion — no puede quedar una tabla legacy huerfana).
    """
    cols_info = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    if not cols_info:
        return  # la tabla no existe todavia; el caller la crea con PK
    has_pk = any(row[5] for row in cols_info)  # row[5] = posicion en la PK (0 = no)
    if has_pk or "id" not in sample_row:
        return  # ya migrada, o sin columna "id" para aplicar PK

    legacy_name = f"{table_name}{_LEGACY_SUFFIX}"
    print(f"  WARN  {table_name:<42} tabla legacy sin PK, migrando (dedup por id)...")
    conn.execute(f'DROP TABLE IF EXISTS "{legacy_name}"')
    conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{legacy_name}"')
    _finish_legacy_migration(conn, legacy_name, table_name)


def recover_orphaned_migrations(conn):
    """Repara tablas "<tabla>__legacy_nopk" huerfanas que pudo dejar una version
    ANTERIOR de este script (cuando RENAME+CREATE+INSERT+DROP todavia no eran
    una unica transaccion atomica y un crash a mitad de camino podia dejar los
    datos originales colgados bajo el nombre legacy, con la tabla destino ya
    creada vacia). Con la version actual esto no puede volver a pasar, pero
    cubre instalaciones que ya quedaron en ese estado.

    Se corre UNA vez al abrir la conexion, antes de sincronizar ninguna tabla,
    y cada reparacion es su propia transaccion atomica (mismo mecanismo que
    `_migrate_legacy_table_if_needed`)."""
    escaped_suffix = _LEGACY_SUFFIX.replace("_", r"\_")
    legacy_tables = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\'",
            (f"%{escaped_suffix}",),
        )
    ]
    for legacy_name in legacy_tables:
        table_name = legacy_name[: -len(_LEGACY_SUFFIX)]
        print(f"  WARN  {table_name:<42} recuperando migracion huerfana ({legacy_name})...")
        conn.execute("BEGIN")
        try:
            _finish_legacy_migration(conn, legacy_name, table_name)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _ensure_schema(conn, table_name, sample_row):
    """Crea la tabla si no existe (con PK en "id"), migra una tabla legacy sin PK,
    y agrega columnas nuevas si el schema del servidor crecio — todo en UNA
    transaccion explicita propia, separada de los datos.

    Se llama UNA SOLA VEZ por tabla, antes del loop de paginas (ver sync_table):
    es DDL puro y es idempotente (una re-ejecucion no encuentra nada para hacer,
    o solo agrega columnas realmente nuevas), asi que queda COMMITEADA antes de
    que arranque la transaccion de datos: un fallo posterior en los datos jamas
    puede dejar el DDL a medio camino (C10 del crack de PLAN-5).
    """
    conn.execute("BEGIN")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone() is not None

        if not exists:
            _create_table_with_pk(conn, table_name, list(sample_row.keys()))
        else:
            _migrate_legacy_table_if_needed(conn, table_name, sample_row)

        _add_missing_columns(conn, table_name, list(sample_row.keys()))
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def upsert_rows(conn, table_name, rows):
    """Inserta/reemplaza filas. No crea la tabla ni commitea: eso lo maneja
    sync_table (_ensure_schema 1 vez + 1 sola transaccion por tabla)."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    col_names = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    sql = f'INSERT OR REPLACE INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
    data = [
        tuple(str(row.get(c)) if row.get(c) is not None else None for c in cols)
        for row in rows
    ]
    conn.executemany(sql, data)
    return len(rows)


def get_last_synced(conn, table_name):
    row = conn.execute(
        "SELECT last_synced_at FROM _sync_meta WHERE table_name = ?", (table_name,)
    ).fetchone()
    return row[0] if row else "2000-01-01T00:00:00Z"


def save_sync_state(conn, table_name, synced_at, new_rows):
    """No commitea: forma parte de la misma transaccion de la tabla (ver sync_table)."""
    current = conn.execute(
        "SELECT total_rows FROM _sync_meta WHERE table_name = ?", (table_name,)
    ).fetchone()
    total = (current[0] or 0) + new_rows if current else new_rows
    conn.execute(
        "INSERT OR REPLACE INTO _sync_meta (table_name, last_synced_at, total_rows) VALUES (?, ?, ?)",
        (table_name, synced_at, total),
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_table(conn, table_name, since, gateway_url, api_key):
    """Sincroniza una tabla completa en dos transacciones explicitas separadas:

    1. DDL (`_ensure_schema`, ver ahi): corre y COMMITEA por su cuenta apenas se
       conoce el sample_row (1ra pagina con filas), antes de tocar datos.
    2. Datos (paginas + `_sync_meta`): UNA transaccion aparte que arranca antes
       de pedir la 1ra pagina y se COMMITEA recien al final. Si algo falla a
       mitad de la paginacion, se hace ROLLBACK completo de esta transaccion
       (no queda watermark de `_sync_meta` adelantado respecto de filas que no
       llegaron a persistir) — el DDL del paso 1 no se ve afectado: ya quedo
       confirmado y es idempotente, asi que la proxima corrida lo encuentra
       hecho y sigue directo a datos.
    """
    page = 1
    page_size = 100
    new_rows = 0
    sync_time = datetime.now(timezone.utc).isoformat()
    table_ready = False  # _ensure_schema se llama una unica vez, con la 1ra pagina con datos

    try:
        conn.execute("BEGIN")
        while True:
            url = (
                f"{gateway_url}/api/v1/sync/data"
                f"?table={table_name}&since={since}&page={page}&page_size={page_size}"
            )
            data = api_get(url, api_key)
            rows = data.get("rows", [])

            if rows:
                if not table_ready:
                    # Nada escrito todavia en esta transaccion de datos: cerrarla
                    # (commit vacio, no-op) para que el DDL corra en la suya propia.
                    conn.execute("COMMIT")
                    _ensure_schema(conn, table_name, rows[0])
                    table_ready = True
                    conn.execute("BEGIN")  # reabrir la transaccion de datos
                new_rows += upsert_rows(conn, table_name, rows)

            if not data.get("has_more", False):
                break
            page += 1

        save_sync_state(conn, table_name, sync_time, new_rows)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return new_rows


def sync_documents(conn, since, gateway_url, api_key, pdf_dir):
    """Descarga PDFs firmados y los guarda en pdf_dir."""
    os.makedirs(pdf_dir, exist_ok=True)
    page = 1
    page_size = 100
    downloaded = 0
    sync_time = datetime.now(timezone.utc).isoformat()

    while True:
        url = (
            f"{gateway_url}/api/v1/sync/documents"
            f"?since={since}&page={page}&page_size={page_size}"
        )
        data = api_get(url, api_key)
        docs = data.get("documents", [])

        for doc in docs:
            official_number = doc.get("official_number", "")
            pdf_url = doc.get("pdf_download_url")
            if not pdf_url:
                continue
            dest = os.path.join(pdf_dir, f"{official_number}.pdf")
            if os.path.exists(dest):
                continue
            try:
                with urllib.request.urlopen(pdf_url, timeout=60) as resp:
                    with open(dest, "wb") as f:
                        f.write(resp.read())
                downloaded += 1
            except Exception as e:
                print(f"  WARN  {official_number:<40} no se pudo descargar: {e}")

        if not data.get("has_more", False):
            break
        page += 1

    # save_sync_state ejecuta un solo statement (INSERT OR REPLACE) fuera de
    # cualquier BEGIN explicito: bajo isolation_level=None se autocommitea solo.
    save_sync_state(conn, "_documents", sync_time, downloaded)
    return downloaded


def main():
    full_sync = "--full" in sys.argv
    with_pdfs = "--pdfs" in sys.argv

    config = load_config()
    pdf_dir = config.get("pdf_dir", "pdfs")
    conn = init_db(config["db_path"])
    recover_orphaned_migrations(conn)

    print(f"\nGDI Sync System v{__version__}")
    check_new_version()
    print(f"  Gateway : {config['gateway_url']}")
    print(f"  DB      : {config['db_path']}")
    modos = []
    if full_sync:
        modos.append("FULL")
    if with_pdfs:
        modos.append("con PDFs")
    print(f"  Modo    : {' + '.join(modos) if modos else 'INCREMENTAL'}\n")

    if full_sync:
        conn.execute("DELETE FROM _sync_meta")  # autocommit (isolation_level=None), sin BEGIN abierto

    # Catalogo de tablas disponibles
    try:
        schema = api_get(f"{config['gateway_url']}/api/v1/sync/schema", config["api_key"])
    except RuntimeError as e:
        print(f"ERROR al conectar con el servidor: {e}")
        sys.exit(1)

    tables = schema.get("tables", [])
    tenant = schema.get("schema_name", "desconocido")
    print(f"  Tenant  : {tenant}")
    print(f"  Tablas  : {len(tables)}\n")

    total_new = 0
    errors = []

    for t in tables:
        name = t["name"]
        since = "2000-01-01T00:00:00Z" if full_sync else get_last_synced(conn, name)
        try:
            n = sync_table(conn, name, since, config["gateway_url"], config["api_key"])
            label = f"+{n} nuevas" if n > 0 else "sin cambios"
            print(f"  {'OK':<4} {name:<42} {label}")
            total_new += n
        except RuntimeError as e:
            print(f"  {'ERR':<4} {name:<42} {e}")
            errors.append(name)

    print(f"\n  Total: {total_new} filas nuevas/actualizadas.")

    # Sync de PDFs (opcional, requiere --pdfs)
    if with_pdfs:
        since_docs = "2000-01-01T00:00:00Z" if full_sync else get_last_synced(conn, "_documents")
        print(f"\n  Sincronizando PDFs en '{pdf_dir}'...")
        try:
            n = sync_documents(conn, since_docs, config["gateway_url"], config["api_key"], pdf_dir)
            print(f"  {'OK':<4} {'documentos (PDFs)':<42} +{n} descargados")
        except RuntimeError as e:
            print(f"  {'ERR':<4} {'documentos (PDFs)':<42} {e}")
            errors.append("documents")

    if errors:
        print(f"\n  Errores en: {', '.join(errors)}")
        sys.exit(1)
    print()


if __name__ == "__main__":
    main()
