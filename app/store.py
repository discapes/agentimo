"""The gateway's own database: restaurants, enrolled keys, signed utterances, orders, chat and the ledger."""
import contextvars
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).with_name("data")
DB = DATA / "aimo.db"

SCHEMA = """
create table if not exists restaurants (id text primary key, name text, profile text);
create table if not exists devices (id text primary key, restaurant_id text, kind text, public_jwk text,
  credential_id text, credential_key blob, sign_count int default 0, created text);
create table if not exists utterances (id text primary key, restaurant_id text, kind text, envelope text,
  signature text, device_id text, audio_path text, text text, words text, created text, consumed_by text,
  revoked int default 0);
create table if not exists orders (id text primary key, restaurant_id text, status text, delivery_date text,
  lines text, instruction_ids text, agent text, note text, verdict text, created text, updated text);
create table if not exists history (restaurant_id text, date text, product_id text, quantity real);
create table if not exists messages (id integer primary key, restaurant_id text, role text, text text,
  utterance_id text, order_id text, created text);
create table if not exists ledger (seq integer primary key, restaurant_id text, ts text, actor text, kind text,
  ref text, body text, prev text, hash text, signer text, sig text);
create index if not exists ledger_restaurant on ledger (restaurant_id, seq);
create table if not exists ledger_cosign (seq integer, hash text, device_id text, sig text, ts text);
create table if not exists tokens (token text primary key, restaurant_id text, agent text);
create index if not exists orders_restaurant on orders (restaurant_id, created);
create index if not exists messages_restaurant on messages (restaurant_id, id);
create index if not exists history_restaurant on history (restaurant_id, product_id);
"""

# The restaurant (browser session) the current request or agent run acts for. Events and ledger entries are
# tagged with it; asyncio tasks and worker threads inherit it.
current = contextvars.ContextVar("restaurant", default=None)

_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        DATA.mkdir(exist_ok=True)
        _db = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA)
    return _db


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def q(sql, *args):
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def run(sql, *args):
    with _lock:
        return db().execute(sql, args)


def insert(table, **row):
    row = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
    run(f"insert into {table} ({','.join(row)}) values ({','.join('?' * len(row))})", *row.values())


def restaurant(rid):
    r = one("select * from restaurants where id=?", rid)
    return r and {**r, "profile": json.loads(r["profile"])}
