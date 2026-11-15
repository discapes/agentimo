"""Valio Aimo's side: offers, substitutes and order intake. Every order goes through the verifier; nothing else
can place one."""
import csv
import json
import math
import re
import secrets
from collections import defaultdict
from datetime import date, datetime

import catalog
import crypto
import events
import ledger
import store
import valio_api

_DATA = catalog.SOURCE.parent
RELIABILITY = {r["productId"]: float(r["deliveryReliability"])
               for r in csv.DictReader(open(_DATA / "valio_aimo_junction26_delivery_reliability.csv"))}
CARBON = {r["productId"]: float(r["AVG kg CO2-eq/kg"])
          for r in csv.DictReader(open(_DATA / "valio_aimo_junction26_carbon_footprint.csv"))}


def _similarity(a, b):
    a, b = _words(a), _words(b)
    return len(a & b) / len(a | b)


def _words(s):
    return set(re.findall(r"[a-z0-9äöå]+", s.lower()))


def _fmt_time(iso):
    return datetime.fromisoformat(iso).strftime("%a %-d %b %H:%M")


def orderability(q, quantity, delivery_date):
    """Why a quoted product can't be delivered on `delivery_date` in `quantity` sales units, or None."""
    if q["price"] is None:
        return "no price for this customer"
    if q["sales_block"]:
        return "sales blocked"
    if q["closing_time"] and datetime.fromisoformat(q["closing_time"]) < datetime.now().astimezone():
        return f"order cut-off passed ({_fmt_time(q['closing_time'])})"
    on_time = [a for a in q["available"] if a["date"] <= delivery_date]
    if not on_time:
        later = [a["date"] for a in q["available"]]
        return f"earliest delivery {later[0]}" if later else "not deliverable"
    if quantity and sum(a["amount"] for a in on_time) < quantity:
        return f"only {sum(a['amount'] for a in on_time):g} available"
    return None


def usual(rid):
    """Average weekly quantity per product over the order history, in sales units."""
    rows = store.q("select product_id, sum(quantity) qty, count(distinct date) n, min(date) first, max(date) last "
                   "from history where restaurant_id=? group by product_id", rid)
    if not rows:
        return {}
    weeks = max(1, (date.fromisoformat(max(r["last"] for r in rows)) -
                    date.fromisoformat(min(r["first"] for r in rows))).days / 7 + 1)
    return {r["product_id"]: {"weekly": round(r["qty"] / weeks, 1), "last": r["last"]} for r in rows}


def _offer(p, q, delivery_date, quantity=None, unit=None, history=None):
    mps = q["measure_per_sales_unit"]
    units = quantity if unit in (None, "sales_units") else (math.ceil(quantity / mps - 1e-9) if mps else None)
    o = {**catalog.summary(p), "price": q["price"], "price_per_unit_measure": q["price_per_unit_measure"],
         "unit_measure": q["unit_measure"], "measure_per_sales_unit": mps, "closing_time": q["closing_time"],
         "logistic_model": q["logistic_model"], "delivery_reliability": RELIABILITY.get(p["id"]),
         "kg_co2e_per_kg": CARBON.get(p["id"])}
    o["problem"] = orderability(q, units, delivery_date)
    if units:
        o["sales_units_needed"], o["line_total"] = units, round(units * q["price"], 2) if q["price"] else None
        o["total_measure"] = round(units * mps, 2) if mps else None
    if history is not None and p["id"] in history:
        o["usual_weekly_sales_units"] = history[p["id"]]["weekly"]
    return o


def find_products(rid, query, delivery_date, quantity=None, unit=None, free_from=(), require_claims=(), limit=8):
    """Valio-side offer search: catalog match, then live price and availability for the shortlist only."""
    hist = usual(rid)
    found = catalog.search(query, require_claims, free_from, limit=30)
    # The restaurant's usual products whose names match the query are always offered, however they rank.
    words = {w[:-1] if len(w) > 5 else w for w in _words(query) if len(w) > 2}
    for pid in hist:
        p = catalog.get(pid)
        names = _words(f"{p['name']} {p['name_fi']}")
        if pid not in {f["id"] for f in found} and any(n.startswith(w) for w in words for n in names) \
                and catalog.passes(p, require_claims, free_from):
            found.append(p)
    if not found:
        return []
    quotes = valio_api.quote([p["id"] for p in found], delivery_date)
    offers = [_offer(p, quotes[p["id"]], delivery_date, quantity, unit, hist) for p in found]
    # When a product the restaurant usually buys can't be delivered, its closest substitutes come next.
    closest = {}
    for o in offers:
        if o["problem"] and "usual_weekly_sales_units" in o:
            for rank, sub in enumerate(substitutes(rid, o["product_id"], delivery_date, quantity, unit, limit=3)):
                if not sub["problem"]:
                    closest.setdefault(sub["product_id"], (rank, o["name"], sub))
    offers = [o for o in offers if o["product_id"] not in closest]
    for pid, (rank, usual_name, sub) in closest.items():
        offers.append({**sub, "closest_to_your_usual": usual_name, "closeness_rank": rank + 1})
    # Orderable first, then usual products, then by how many of the chef's words the name matches, then closeness
    # to an undeliverable usual product, then price.
    matches = lambda o: sum(any(n.startswith(w) for n in _words(o["name"])) for w in words)
    offers.sort(key=lambda o: (o["problem"] is not None, "usual_weekly_sales_units" not in o, -matches(o),
                               o.get("closeness_rank", 99), o["price_per_unit_measure"] or 1e9))
    return offers[:limit]


def check_offers(rid, product_ids, delivery_date):
    quotes = valio_api.quote(product_ids, delivery_date)
    hist = usual(rid)
    return [_offer(catalog.get(pid), quotes[pid], delivery_date, history=hist) for pid in product_ids if catalog.get(pid)]


def substitutes(rid, product_id, delivery_date, quantity=None, unit=None, limit=6):
    """Ranked substitutes within Valio Aimo's assortment, each with a field-by-field comparison to the original.
    Candidates come from Valio's substitution list, the restaurant's purchase history and products in the same
    category with the same free-from and diet claims."""
    orig = catalog.get(product_id)
    if not orig:
        return []
    hist = usual(rid)
    keep = {c for c in orig["claims"] if c.startswith("FREE_FROM_") or c in ("VEGAN", "VEGETARIAN")}
    free = {k for k, v in orig["allergens"].items() if v == "FREE_FROM"}
    pool = {pid for pid in orig["substitutes"]} | {pid for pid in hist if pid != product_id}
    pool |= {r["id"] for r in catalog.db().execute("select id from products where category=? and id<>?",
                                                   (orig["category"], product_id))}
    ow = _words(orig["name"])
    cands = []
    for pid in pool:
        p = catalog.get(pid)
        if not p or p["category"] != orig["category"] and pid not in orig["substitutes"]:
            continue
        if not keep <= set(p["claims"]) or any(p["allergens"].get(a) != "FREE_FROM" for a in free):
            continue
        sim = len(ow & _words(p["name"])) / len(ow | _words(p["name"]))
        cands.append((pid in orig["substitutes"], pid in hist, sim, p))
    cands.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    cands = cands[:25]
    quotes = valio_api.quote([product_id] + [c[3]["id"] for c in cands], delivery_date)
    oq = quotes[product_id]
    out = []
    for listed, bought, sim, p in cands:
        q = quotes[p["id"]]
        if q["unit_measure"] != oq["unit_measure"]:
            continue
        o = _offer(p, q, delivery_date, quantity, unit, hist)
        o["comparison"] = {
            "valio_listed_substitute": listed, "bought_before": bought, "name_similarity": round(sim, 2),
            "same_brand": p["brand"] == orig["brand"],
            "price_per_unit_measure_change_pct": round(100 * (q["price_per_unit_measure"] / oq["price_per_unit_measure"] - 1))
            if q["price_per_unit_measure"] and oq["price_per_unit_measure"] else None,
            "claims_lost": sorted(set(orig["claims"]) - set(p["claims"])),
            "claims_gained": sorted(set(p["claims"]) - set(orig["claims"])),
            "allergens_differ": sorted({k for k in set(orig["allergens"]) | set(p["allergens"])
                                        if orig["allergens"].get(k) != p["allergens"].get(k)}),
        }
        out.append(o)
    out.sort(key=lambda o: (o["problem"] is not None, not o["comparison"]["valio_listed_substitute"],
                            not o["comparison"]["bought_before"],
                            len(o["comparison"]["claims_lost"]) + len(o["comparison"]["claims_gained"]),
                            -o["comparison"]["name_similarity"]))
    return out[:limit]


def order_history(rid):
    """What the restaurant usually buys per week, in sales units and in litres or kilograms (from live prices)."""
    from datetime import date, timedelta
    hist = usual(rid)
    quotes = valio_api.quote(list(hist), (date.today() + timedelta(days=1)).isoformat())
    out = []
    for pid, h in hist.items():
        q, p = quotes[pid], catalog.get(pid)
        row = {"product_id": pid, "name": p["name"], "sales_unit": p["sales_unit"],
               "usual_weekly_sales_units": h["weekly"], "last_ordered": h["last"]}
        if q["measure_per_sales_unit"]:
            row["usual_weekly_amount"] = round(h["weekly"] * q["measure_per_sales_unit"], 1)
            row["unit"] = q["unit_measure"]
        out.append(row)
    return out


# Orders

def order_hash(o):
    """What the chef's approval signs: the lines that will be delivered and the total."""
    lines = [{"product_id": l["product_id"], "quantity": l["quantity"]} for l in o["lines"] if not l.get("removed")]
    return crypto.sha256(crypto.canonical({"id": o["id"], "delivery_date": o["delivery_date"], "lines": lines,
                                           "total": total(o)}))


def total(o):
    return round(sum(l.get("line_total") or 0 for l in o["lines"] if not l.get("removed")), 2)


def get_order(oid):
    o = store.one("select * from orders where id=?", oid)
    if not o:
        return None
    for k in ("lines", "instruction_ids", "verdict"):
        o[k] = json.loads(o[k]) if o[k] else None
    o["total"] = total(o)
    o["hash"] = order_hash(o)
    return o


def save_order(o, **changes):
    o.update(changes)
    store.run("update orders set status=?, lines=?, verdict=?, updated=? where id=?", o["status"],
              json.dumps(o["lines"], ensure_ascii=False), json.dumps(o["verdict"], ensure_ascii=False),
              store.now(), o["id"])
    events.emit("order", order_id=o["id"], status=o["status"])


def submit_order(rid, agent, delivery_date, lines, note=None):
    """Takes a draft order from any agent and runs it through the verifier. The agent never approves."""
    import verifier
    bad = [l["product_id"] for l in lines if not catalog.get(str(l["product_id"]))]
    if bad:
        return {"error": f"unknown product ids: {bad}"}
    oid = "A-" + secrets.token_hex(3).upper()
    clean = []
    for l in lines:
        p = catalog.get(str(l["product_id"]))
        clean.append({"product_id": p["id"], "name": p["name"], "sales_unit": p["sales_unit"],
                      "quantity": float(l["quantity"]), "cites": l.get("cites") or []})
    instruction_ids = sorted({c["instruction_id"] for l in clean for c in l["cites"] if c.get("instruction_id")})
    o = {"id": oid, "restaurant_id": rid, "status": "verifying", "delivery_date": delivery_date, "lines": clean,
         "instruction_ids": instruction_ids, "agent": agent, "note": note, "verdict": None,
         "created": store.now(), "updated": store.now()}
    store.insert("orders", **o)
    events.emit("order.submitted", order_id=oid, agent=agent, lines=len(clean))
    ledger.append(agent, "order.submitted", oid, {"agent": agent, "delivery_date": delivery_date, "note": note,
                  "lines": [{k: l[k] for k in ("product_id", "quantity", "cites")} for l in clean]},
                  signer="agent" if agent == "aimo-agent" else "gateway")
    verifier.verify(get_order(oid))
    return order_summary(get_order(oid))


def cancel(rid, order_id, instruction_id):
    """Cancels an order the chef asked to cancel or change in a new signed instruction. Only an order waiting for
    approval, or placed within the last minute, can be cancelled. Gemini Decisions confirms the request."""
    import verifier
    o = get_order(order_id)
    if not o or o["restaurant_id"] != rid:
        return {"error": "no such order"}
    placed_recently = o["status"] == "placed" and (datetime.now().astimezone() - datetime.fromisoformat(o["updated"])).total_seconds() < 65
    if o["status"] != "held" and not placed_recently:
        return {"error": f"order is {o['status']} and can no longer be cancelled"}
    u = store.one("select * from utterances where id=? and restaurant_id=? and kind='instruction'", instruction_id, rid)
    problem = verifier.utterance_problem(u, rid, order_id) if u else "not found"
    if problem and not problem.startswith("already used"):
        return {"error": f"instruction {problem}"}
    if verifier.asks_to_cancel(u, o) < 0.7:
        return {"error": "the chef's words don't ask to cancel or change this order"}
    save_order(o, status="cancelled")
    ledger.append("chef", "order.cancelled", o["id"], {"order_hash": o["hash"], "instruction": instruction_id})
    return {"order_id": o["id"], "status": "cancelled"}


def place(o, by):
    """Sends the order to the warehouse and marks its instructions used, so they can't back another order."""
    for iid in o["instruction_ids"]:
        store.run("update utterances set consumed_by=? where id=?", o["id"], iid)
    save_order(o, status="placed")
    ledger.append(by, "order.placed", o["id"], {"order_hash": o["hash"], "total_eur": o["total"],
                  "lines": [{"product_id": l["product_id"], "quantity": l["quantity"]}
                            for l in o["lines"] if not l.get("removed")],
                  "delivery_date": o["delivery_date"]})
    events.emit("warehouse", order_id=o["id"], total=o["total"])


def order_summary(o):
    """What the agent sees after submitting: the decision and its reasons, never a way to approve."""
    v = o["verdict"] or {}
    return {"order_id": o["id"], "status": o["status"], "total_eur": o["total"], "delivery_date": o["delivery_date"],
            "decision": v.get("decision"), "reasons": v.get("reasons", []),
            "lines": [{"line": i + 1, "product_id": l["product_id"], "name": l["name"], "quantity": l["quantity"],
                       "sales_unit": l["sales_unit"], "line_total_eur": l.get("line_total"),
                       "flags": [f["text"] for f in l.get("flags", [])]} for i, l in enumerate(o["lines"])],
            "next": {"placed": "The order was sent to Valio Aimo's warehouse.",
                     "held": "The order waits for the chef's approval on their phone. Tell the chef briefly why.",
                     "rejected": "The order was refused. Do not retry with the same instructions."}.get(o["status"], "")}
