"""Client for Valio Aimo's mock price and availability APIs."""
import re
from pathlib import Path

import httpx

BASE = "https://junction26.valioaimo.fi/api"
KEY = re.search(r"VALIO_API_KEY=(\S+)", (Path.home() / ".config/junction/valio.env").read_text())[1].strip("'\"")
# The API's firewall refuses Python's default user agent.
_client = httpx.Client(headers={"X-API-Key": KEY, "User-Agent": "aimo-gateway/1"}, timeout=20)


def _snap(x):
    # Comparison prices are rounded to cents, so 6.86 / 1.37 gives 5.007 litres for a case of five 1 l cartons.
    return round(x * 20) / 20 if x >= 1 else round(x, 3)


def _post(path, date, ids):
    r = _client.post(f"{BASE}/{path}", json={"customer": "aimo", "requestedDate": date, "products": list(ids)})
    r.raise_for_status()
    return {p["productId"]: p for p in r.json()["products"]}


def quote(ids, date):
    """Price and availability per product for delivery on `date` (YYYY-MM-DD), merged into one dict per id."""
    ids = list(dict.fromkeys(ids))
    prices, avail = _post("price", date, ids), _post("availability", date, ids)
    out = {}
    for pid in ids:
        p, a = prices.get(pid) or {}, avail.get(pid) or {}
        cat, cmp = (p.get("catalogPrice") or {}).get("value"), (p.get("comparisonPrice") or {}).get("value")
        lines = a.get("scheduleLines") or []
        out[pid] = {
            "price": cat, "price_per_unit_measure": cmp, "unit_measure": p.get("comparisonUnit"),
            # Litres or kilograms in one sales unit, from the ratio of sales-unit price to comparison price.
            "measure_per_sales_unit": _snap(cat / cmp) if cat and cmp else None,
            "vat": (p.get("tax") or {}).get("taxRate"),
            "closing_time": a.get("closingTime"), "logistic_model": a.get("logisticModel"),
            "sales_block": a.get("salesBlock", False),
            # Schedule lines without a date or amount occur in the API's answers; they promise nothing.
            "available": [{"date": l["availableDate"], "amount": l.get("availableAmount") or 0, "unit": l.get("salesUnit")}
                          for l in lines if not l.get("salesBlock") and l.get("availableDate")],
        }
    return out
