import sqlite3
con = sqlite3.connect("jobs.db")
cur = con.cursor()

try:
    cur.execute("drop table chatids")
except Exception:
    pass

cur.execute("CREATE TABLE chatids(id type unique, hour, min)")