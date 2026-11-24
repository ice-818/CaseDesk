import sqlite3
import sys

session_id = sys.argv[1]
con = sqlite3.connect(r"D:\code\CaseDesk\data\local\app_full.db")
row = con.execute("select user_id from sessions where session_id=?", (session_id,)).fetchone()
print(row[0] if row else "session-not-found")
