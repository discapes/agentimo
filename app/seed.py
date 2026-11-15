"""Demo restaurants. Every browser session is its own restaurant with the same profile and 12 weeks of purchase
history (the challenge data has none). `laituri` is the fixed restaurant for scripts and outside agents.

    python seed.py ensure    # create laituri
    python seed.py reset     # delete all sessions"""
import json
import random
import secrets
import sys
from datetime import date, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import crypto
import store

RID = "laituri"
PROFILE = {"name": "Ravintola Laituri", "city": "Espoo", "chef": "Mikko", "seats": 70,
           "kitchen": "Seaside lunch restaurant: salmon soup, fish of the day, a vegetarian dish, coffee and cake."}
# Usual weekly quantities in sales units, split over Monday and Thursday deliveries.
WEEKLY = {
    "465039": 18,   # Valio Eila semi skimmed milk drink 1 l lactose free, case of 5 (90 l)
    "464194": 24,   # Valio whipping cream 1 l UHT lactose free
    "464411": 20,   # Valio butter 500 g unsalted
    "461814": 25,   # salmon fillet C-cut deboned, kg
    "460478": 60,   # potato washed 1 kg
    "462379": 6,    # peeled onion 2 kg
    "460395": 15,   # dill bunch 100 g
    "460846": 4,    # lemon 1 kg
    "476743": 3,    # Valio A+ natural yoghurt 5 kg lactose free
    "472299": 6,    # Mövenpick filter coffee 1 kg
    "473485": 12,   # Oatly iKaffe 1 l
    "474454": 2,    # jasmine rice 5 kg
}
RULES = ("Milk and cream must always be lactose free. Any brand or pack size of lactose-free milk is fine. "
         "Never substitute the salmon: if it is not available, ask me.")
LIMITS = {"auto_eur": 2500, "max_eur": 10000, "autonomy": "deviations"}


_ready = set()


def ensure(rid):
    """Creates restaurant `rid` on first use: the demo profile, 12 weeks of history and its agent tokens."""
    if rid in _ready:
        return
    with store._lock:
        if not store.one("select 1 from restaurants where id=?", rid):
            store.insert("restaurants", id=rid, name=PROFILE["name"], profile=PROFILE)
            rnd = random.Random(7)
            monday = date.today() - timedelta(days=date.today().weekday() + 7 * 12)
            rows = []
            for week in range(12):
                for day, share in ((0, 0.6), (3, 0.4)):
                    d = (monday + timedelta(weeks=week, days=day)).isoformat()
                    rows += [(rid, d, pid, n) for pid, qty in WEEKLY.items() if (n := round(qty * share * rnd.uniform(0.8, 1.2)))]
            store.db().execute("begin")
            store.db().executemany("insert into history (restaurant_id, date, product_id, quantity) values (?,?,?,?)", rows)
            store.db().executemany("insert into tokens values (?,?,?)",
                                   [(secrets.token_urlsafe(18), rid, a) for a in ("aimo-agent", "external-agent")])
            store.db().execute("commit")
    _ready.add(rid)


def token(rid, agent):
    ensure(rid)
    return store.one("select token from tokens where restaurant_id=? and agent=?", rid, agent)["token"]


def reset():
    """Deletes every browser session's restaurant and everything in it. Open apps start a new session on reload."""
    for t in ("restaurants", "history", "tokens", "devices", "utterances", "orders", "messages", "ledger", "ledger_cosign"):
        store.run(f"delete from {t}")
    print("cleared all sessions")


def test_device(rid=RID):
    """A P-256 key held by the server, standing in for the phone in tests and the attack lab, enrolled in `rid`."""
    path = crypto.KEYS / "test-device.pem"
    if not path.exists():
        crypto.KEYS.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key = serialization.load_pem_private_key(path.read_bytes(), None)
    n = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": crypto.b64(n.x.to_bytes(32)).rstrip("="), "y": crypto.b64(n.y.to_bytes(32)).rstrip("=")}
    did = f"lab-{rid}"
    if not store.one("select 1 from devices where id=?", did):
        store.insert("devices", id=did, restaurant_id=rid, kind="device", public_jwk=jwk, created=store.now())
    return key, did


if __name__ == "__main__":
    {"reset": reset, "ensure": lambda: ensure(RID)}[sys.argv[1]]()
