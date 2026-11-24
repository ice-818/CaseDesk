import os
import sqlite3

db = os.path.join(os.environ["TEMP"], "casedesk_0009_verify.db")
con = sqlite3.connect(db)
con.execute("PRAGMA foreign_keys=ON")
cur = con.cursor()

def q1(sql):
    return cur.execute(sql).fetchone()[0]

print("version:", q1("SELECT version_num FROM alembic_version"))
print("merchants:", cur.execute("SELECT merchant_id,status FROM merchants").fetchall())
print("products merchant counts:", cur.execute("SELECT merchant_id,count(*) FROM products GROUP BY merchant_id").fetchall())
print("orders merchant counts:", cur.execute("SELECT merchant_id,count(*) FROM orders GROUP BY merchant_id").fetchall())
print("sessions:", cur.execute("SELECT channel,merchant_id FROM sessions").fetchall())
print("service agent:", cur.execute("SELECT user_id,merchant_id FROM users WHERE role='service_agent'").fetchall())
print("indexes:", [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%merchant%'").fetchall()])

# CHECK enforcement: these inserts must all fail
attempts = [
    ("shop session needs merchant", "INSERT INTO sessions(session_id,user_id,channel,merchant_id) VALUES('s_bad','u_demo','shop',NULL)"),
    ("platform session must be null", "INSERT INTO sessions(session_id,user_id,channel,merchant_id) VALUES('s_bad2','u_demo','platform','m_demo')"),
    ("bad channel", "INSERT INTO sessions(session_id,user_id,channel,merchant_id) VALUES('s_bad3','u_demo','wechat',NULL)"),
    ("service agent needs merchant", "INSERT INTO users(user_id,role,status,merchant_id) VALUES('u_bad','service_agent','active',NULL)"),
    ("bad merchant status", "INSERT INTO merchants(merchant_id,name,status,created_at,updated_at) VALUES('m_x','x','paused','2026-01-01','2026-01-01')"),
]
for label, sql in attempts:
    try:
        cur.execute(sql)
        con.commit()
        print(f"CHECK FAIL ({label}): insert was accepted!")
    except sqlite3.IntegrityError:
        print(f"OK blocked ({label})")

# FK enforcement: product pointing at nonexistent merchant must fail
try:
    cur.execute("INSERT INTO products(sku,name,merchant_id) VALUES('SKU-BAD','bad','m_nope')")
    con.commit()
    print("FK FAIL: orphan product accepted")
except sqlite3.IntegrityError:
    print("OK blocked (orphan product FK)")

con.close()
print("DONE")
