"""The ERP: vendor master and open purchase orders, in SQLite (erp.db, created on first use)."""
import sqlite3
from pathlib import Path

DB = Path(__file__).with_name("erp.db")

VENDORS = [
    # vendor, bank, account on file, routing
    ("Northwind Metals", "First Lakeshore Bank", "009811234471", "071000013"),
    ("Brightline Logistics", "Pacific Union Bank", "551208838812", "121000358"),
    ("Kestrel Packaging", "Granite State Bank", "300417762205", "011400495"),
    ("Orion Industrial Gases", "Bayview Commerce", "870055129034", "026009593"),
    ("Vale Electrical", "Midland Trust", "412290067718", "081000210"),
    ("Summit Facility Services", "Cascade National", "665031904456", "125000024"),
]
POS = [
    # po, vendor, amount
    ("PO-77120", "Northwind Metals", 184500.00),
    ("PO-77131", "Brightline Logistics", 42180.00),
    ("PO-77140", "Kestrel Packaging", 18960.50),
    ("PO-77152", "Orion Industrial Gases", 9875.00),
    ("PO-77166", "Vale Electrical", 63240.00),
    ("PO-77171", "Summit Facility Services", 27400.00),
]


def connect():
    fresh = not DB.exists()
    db = sqlite3.connect(DB)
    if fresh:
        db.executescript("""
            CREATE TABLE vendors (name TEXT PRIMARY KEY, bank TEXT, account TEXT, routing TEXT);
            CREATE TABLE purchase_orders (po TEXT PRIMARY KEY, vendor TEXT, amount REAL, status TEXT);
        """)
        db.executemany("INSERT INTO vendors VALUES (?, ?, ?, ?)", VENDORS)
        db.executemany("INSERT INTO purchase_orders VALUES (?, ?, ?, 'open')", POS)
        db.commit()
    return db


class ERP:
    def __init__(self):
        self.db = connect()

    def vendor(self, name):
        r = self.db.execute("SELECT name, bank, account, routing FROM vendors WHERE name = ?", (name,)).fetchone()
        return dict(zip(("name", "bank", "account", "routing"), r)) if r else None

    def account_on_file(self, name):
        v = self.vendor(name)
        return v["account"] if v else None

    def open_po(self, po):
        r = self.db.execute("SELECT vendor, amount FROM purchase_orders WHERE po = ? AND status = 'open'", (po,)).fetchone()
        return {"vendor": r[0], "amount": r[1]} if r else None

    def close_po(self, po):
        self.db.execute("UPDATE purchase_orders SET status = 'paid' WHERE po = ?", (po,))
        self.db.commit()

    def reset(self):
        self.db.execute("UPDATE purchase_orders SET status = 'open'")
        self.db.commit()
