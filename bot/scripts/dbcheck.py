import sqlite3

c = sqlite3.connect("data/melloky.db")
print(
    "tables:",
    [r[0] for r in c.execute("select name from sqlite_master where type='table'")],
)
print("users:", c.execute("select count(*) from users").fetchone()[0])
print("plans:", c.execute("select title, price_rub from plans").fetchall())
print("docs:", c.execute("select key, value from kv where key like '%:url'").fetchall())
