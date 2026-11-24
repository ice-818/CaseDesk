"""Verify migration 0010: after-sale status CHECK + partial unique index.

Runs entirely on a disposable database in TEMP; the development database is
never touched. Exits non-zero if any assertion fails.
"""

import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[3]
db_path = Path(os.environ["TEMP"]) / "casedesk_0010_verify.db"
if db_path.exists():
    db_path.unlink()

engine = create_engine(f"sqlite:///{db_path}")
connection = engine.connect()
config = Config(str(ROOT / "alembic.ini"))
config.set_main_option("script_location", str(ROOT / "alembic"))
config.attributes["connection"] = connection
command.upgrade(config, "0010")

con = sqlite3.connect(db_path)
con.execute("PRAGMA foreign_keys=ON")
cur = con.cursor()

checks: list[tuple[bool, str, str]] = []


def expect_blocked(label: str, sql: str) -> None:
    try:
        cur.execute(sql)
        con.commit()
        checks.append((False, label, "was accepted"))
    except sqlite3.IntegrityError:
        checks.append((True, label, "blocked"))


def expect_allowed(label: str, sql: str) -> None:
    try:
        cur.execute(sql)
        con.commit()
        checks.append((True, label, "allowed"))
    except sqlite3.IntegrityError as exc:
        checks.append((False, label, str(exc)))


print("version:", cur.execute("SELECT version_num FROM alembic_version").fetchone()[0])
indexes = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}
print("guard indexes present:", [n for n in ("uq_refunds_active_order", "uq_returns_active_order") if n in indexes])

# Refunds: one active document per order.
expect_allowed("first active refund", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf1','ORD1','requested',1000,'2026-01-01 00:00:00')")
expect_blocked("second active refund blocked", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf2','ORD1','approved',1000,'2026-01-01 00:00:00')")
expect_allowed("active refund on another order", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf3','ORD2','requested',500,'2026-01-01 00:00:00')")
expect_allowed("rejected refund coexists", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf4','ORD1','rejected',1000,'2026-01-01 00:00:00')")
expect_allowed("completed refund coexists", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf5','ORD1','completed',1000,'2026-01-01 00:00:00')")
expect_blocked("invalid refund status blocked", "INSERT INTO refunds(refund_id,order_id,status,amount_cents,created_at) VALUES('rf6','ORD9','bogus',1,'2026-01-01 00:00:00')")

# Returns: same guard.
expect_allowed("first active return", "INSERT INTO returns(return_id,order_id,status,created_at) VALUES('rt1','ORD1','requested','2026-01-01 00:00:00')")
expect_blocked("second active return blocked", "INSERT INTO returns(return_id,order_id,status,created_at) VALUES('rt2','ORD1','approved','2026-01-01 00:00:00')")
expect_blocked("invalid return status blocked", "INSERT INTO returns(return_id,order_id,status,created_at) VALUES('rt3','ORD9','nope','2026-01-01 00:00:00')")

for ok, label, detail in checks:
    print(("OK   " if ok else "FAIL ") + label + " - " + detail)

con.close()

# Downgrade restores 0009 and removes the guard.
command.downgrade(config, "0009")
con = sqlite3.connect(db_path)
cur = con.cursor()
print("after downgrade version:", cur.execute("SELECT version_num FROM alembic_version").fetchone()[0])
indexes = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}
print("guard indexes removed:", not any(n in indexes for n in ("uq_refunds_active_order", "uq_returns_active_order")))
con.close()
connection.close()
engine.dispose()
db_path.unlink()

if not all(ok for ok, _, _ in checks):
    sys.exit(1)
print("ALL PASS")
