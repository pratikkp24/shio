"""A small in-memory shop database for the SQL example."""
import sqlite3

from shio.models import ollama, replay


def open_db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL, placed_on TEXT);
        INSERT INTO customers VALUES (1, 'Asha', 'Pune'), (2, 'Ben', 'Austin'), (3, 'Chen', 'Austin');
        INSERT INTO orders VALUES (1, 1, 1200, '2026-09-02'), (2, 2, 300, '2026-09-05'), (3, 2, 450, '2026-10-01'),
                                  (4, 3, 80, '2026-10-03'), (5, 1, 990, '2026-10-09');
    """)
    return db


def schema(db):
    return "\n".join(r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type = 'table'"))


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def explains(db, sql):
    """True if SQLite can plan the query (so it parses and every table and column exists)."""
    db.execute("EXPLAIN " + sql)
    return True


def run(db, sql):
    cur = db.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def model(name):
    """`replay` answers like a model that was talked into a destructive query."""
    if name == "replay":
        return replay(lambda req: {"sql": "DELETE FROM orders", "tables": ["orders"]}, name="replay")
    return ollama(name)
