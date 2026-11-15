"""Valio Aimo product catalog: built once from the static product file into SQLite with full-text search."""
import json
import sqlite3
import sys
from pathlib import Path

SOURCE = Path.home() / "junction-data/valio/valio_aimo_junction26_product_data.json"
DB = Path(__file__).with_name("data") / "catalog.db"

# GS1 allergen codes that occur in the data.
ALLERGENS = {
    "AM": "milk", "ML": "lactose", "AW": "gluten", "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye",
    "AE": "egg", "AN": "tree nuts", "AP": "peanuts", "AY": "soy", "AS": "sesame", "AF": "fish",
    "AC": "crustaceans", "UM": "molluscs", "AU": "sulphites", "BC": "celery", "BM": "mustard", "NL": "lupin",
    "SA": "almonds", "SH": "hazelnuts", "SW": "walnuts", "SC": "cashews", "ST": "pistachios", "GS": "spelt",
}

SCHEMA = """
create table products (id text primary key, name text, name_fi text, name_sv text, brand text, vendor text,
  category text, sales_unit text, base_unit text, units text, claims text, allergens text, labels text,
  origin text, temperature int, description text, ingredients text, substitutes text);
create virtual table products_fts using fts5(id unindexed, name, name_fi, name_sv, description, tokenize='unicode61 remove_diacritics 2');
"""


def _text(items, lang="en"):
    return next((t["value"] for t in items or [] if t.get("language") == lang), "")


def build():
    items = json.loads(SOURCE.read_text())
    subs = {}
    for i in items:
        for s in i.get("substitutions") or []:
            subs.setdefault(s["product"], []).append(s["substituteProduct"])
    DB.parent.mkdir(exist_ok=True)
    DB.unlink(missing_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    for i in items:
        s = i["synkkaData"]
        cls = {c["name"]: c["values"] for c in s.get("classifications", [])}
        allergens = {v["id"]: v.get("unit") for v in cls.get("nonAllergen", [])}
        allergens |= {v["id"]: v.get("unit") for v in cls.get("allergen", [])}
        row = dict(
            id=i["id"], name=_text(s["names"]) or _text(s["names"], "fi"), name_fi=_text(s["names"], "fi"),
            name_sv=_text(s["names"], "sv"), brand=s.get("brand"), vendor=i.get("vendorName"),
            category=i["category"], sales_unit=i["salesUnit"], base_unit=i["baseUnit"], units=json.dumps(i["units"]),
            claims=json.dumps([v["id"] for v in cls.get("nutritionalClaim", [])]),
            allergens=json.dumps(allergens), labels=json.dumps([v["id"] for v in cls.get("packagingMarkedLabel", [])]),
            origin=i.get("countryOfOrigin"), temperature=i.get("temperatureCondition"),
            description=_text(s.get("marketingTexts")), ingredients=_text(s.get("keyIngredients")),
            substitutes=json.dumps(subs.get(i["id"], [])))
        db.execute(f"insert into products values ({','.join('?' * len(row))})", list(row.values()))
        db.execute("insert into products_fts values (?,?,?,?,?)",
                   (row["id"], row["name"], row["name_fi"], row["name_sv"], row["description"]))
    db.commit()
    print(f"{len(items)} products -> {DB}")


_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB, check_same_thread=False)
        _db.row_factory = sqlite3.Row
    return _db


def _product(row):
    p = dict(row)
    for k in ("units", "claims", "allergens", "labels", "substitutes"):
        p[k] = json.loads(p[k])
    return p


def get(pid):
    row = db().execute("select * from products where id=?", (pid,)).fetchone()
    return _product(row) if row else None


CODES = {v: k for k, v in ALLERGENS.items()}


def passes(p, require_claims=(), free_from=()):
    """Whether a product has the required claims and is stated free from the given allergens."""
    return all(c in p["claims"] for c in require_claims) and all(
        p["allergens"].get(CODES.get(a, a)) == "FREE_FROM" or (a == "lactose" and "FREE_FROM_LACTOSE" in p["claims"])
        for a in free_from)


def search(query, require_claims=(), free_from=(), category=None, limit=20):
    """Full-text search over names and descriptions. `free_from` takes allergen names; a product passes only
    when the data states it is free from the allergen (unknown counts as not free)."""
    # Long words match on their stem, so "jasmine" also finds "JASMIN" and "potatoes" finds "potato".
    terms = " ".join(f'"{t[:-1] if len(t) > 5 else t}"*' for t in query.replace('"', " ").split())
    rows = db().execute(
        "select p.* from products_fts f join products p on p.id=f.id where products_fts match ? "
        "order by bm25(products_fts, 0, 10, 10, 10, 1) limit 400", (terms,)).fetchall()
    out = []
    for r in rows:
        p = _product(r)
        if category and not p["category"].startswith(category) or not passes(p, require_claims, free_from):
            continue
        out.append(p)
        if len(out) == limit:
            break
    return out


# Planted product texts per restaurant (browser session) for the attack demo: a stand-in for a manufacturer's
# description written to manipulate purchasing agents.
PLANTED = {}


def description(p):
    import store
    return PLANTED.get(store.current.get(), {}).get(p["id"], p["description"])


def summary(p):
    """The fields an agent needs to compare products."""
    contains = [ALLERGENS.get(k, k) for k, v in p["allergens"].items() if v == "CONTAINS"]
    return {"product_id": p["id"], "name": p["name"], "brand": p["brand"], "category": p["category"],
            "sales_unit": p["sales_unit"], "claims": p["claims"], "contains": contains,
            "origin": p["origin"], "description": description(p)[:500]}


if __name__ == "__main__" and sys.argv[1:] == ["build"]:
    build()
