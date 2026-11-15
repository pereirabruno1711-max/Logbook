import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, name TEXT, gpswox_hash TEXT,
    trusttrack_key TEXT, is_admin INTEGER DEFAULT 0, api_token TEXT UNIQUE, created_at TEXT, last_login TEXT,
    password_hash TEXT, must_change INTEGER DEFAULT 0, pw_changed_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, expires TEXT, company_id INTEGER, as_role TEXT);
CREATE TABLE IF NOT EXISTS companies(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, nif TEXT, gpswox_hash TEXT, trusttrack_key TEXT,
    api_token TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS members(company_id INTEGER, user_id INTEGER, role TEXT DEFAULT 'gestor', created_at TEXT,
    PRIMARY KEY(company_id, user_id));
CREATE TABLE IF NOT EXISTS invites(company_id INTEGER, email TEXT, role TEXT, created_at TEXT, PRIMARY KEY(company_id, email));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- equipamentos GPS lidos das plataformas (um "objeto" é o que se associa ao motorista)
CREATE TABLE IF NOT EXISTS devices(
    company_id INTEGER, provider TEXT, device_id TEXT, name TEXT, plate TEXT, updated_at TEXT,
    online TEXT, last_time TEXT, lat REAL, lon REAL, PRIMARY KEY(company_id, provider, device_id));

CREATE TABLE IF NOT EXISTS drivers(
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, name TEXT, phone TEXT, licence TEXT,
    token TEXT UNIQUE, active INTEGER DEFAULT 1, created_at TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_drv_co ON drivers(company_id);

-- objeto atribuído ao motorista, com histórico (to_ts NULL = atual)
CREATE TABLE IF NOT EXISTS assignments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id INTEGER, provider TEXT, device_id TEXT, device_name TEXT,
    plate TEXT, from_ts TEXT, to_ts TEXT);
CREATE INDEX IF NOT EXISTS ix_as_drv ON assignments(driver_id);

-- estado escolhido na app (Trabalho / Descanso)
CREATE TABLE IF NOT EXISTS driver_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id INTEGER, ts TEXT, state TEXT);
CREATE INDEX IF NOT EXISTS ix_ev_drv ON driver_events(driver_id, ts);

-- um relatório por motorista e dia: sem linha = "Sem relatório"; filling = "Em preenchimento"; signed = "Assinado"
CREATE TABLE IF NOT EXISTS logbooks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, driver_id INTEGER, day TEXT, status TEXT DEFAULT 'filling',
    data TEXT, created_at TEXT, updated_at TEXT, signed_at TEXT, signature TEXT, snapshot TEXT,
    pdf_path TEXT, pdf_sha256 TEXT, signed_ip TEXT, UNIQUE(company_id, driver_id, day));
CREATE INDEX IF NOT EXISTS ix_lb_co ON logbooks(company_id, day);

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, driver_id INTEGER, day TEXT, kind TEXT, detail TEXT,
    created_at TEXT, dismissed INTEGER DEFAULT 0, UNIQUE(company_id, driver_id, day, kind));
CREATE TABLE IF NOT EXISTS odometer_log(
    company_id INTEGER, provider TEXT, device_id TEXT, snap_day TEXT, km REAL, ts TEXT,
    PRIMARY KEY(company_id, provider, device_id, snap_day));
CREATE TABLE IF NOT EXISTS driver_notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, driver_id INTEGER, ts TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS ix_note_drv ON driver_notes(driver_id, ts);
CREATE TABLE IF NOT EXISTS geocache(k TEXT PRIMARY KEY, name TEXT);
"""


def connect():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def tx():
    c = connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    for d in (config.DATA_DIR, config.PDF_DIR):
        os.makedirs(d, exist_ok=True)
    with tx() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(devices)").fetchall()]
        for col, typ in (("odometer", "REAL"), ("odo_key", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE devices ADD COLUMN {col} {typ}")
        dcols = [r["name"] for r in c.execute("PRAGMA table_info(drivers)").fetchall()]
        if "can_pick_vehicle" not in dcols:
            c.execute("ALTER TABLE drivers ADD COLUMN can_pick_vehicle INTEGER DEFAULT 0")


def claim_orphans(company_id):  # compatibilidade com auth.py (não há dados antigos a reclamar)
    return None


def q(sql, *args):
    with tx() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, *args):
    r = q(sql, *args)
    return r[0] if r else None


def run(sql, *args):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid
