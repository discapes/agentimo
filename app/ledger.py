"""Append-only order log shared by the restaurant and Valio Aimo. Each entry holds the hash of the previous one
and is signed by the service that wrote it; the chef's phone co-signs the head, so neither side can rewrite
history alone."""
import json

import crypto
import events
import store

GENESIS = "0" * 64


def _entry_bytes(e):
    return crypto.canonical({k: e[k] for k in ("seq", "restaurant_id", "ts", "actor", "kind", "ref", "body", "prev")})


def append(actor, kind, ref, body, signer="gateway"):
    """Appends to the current restaurant's chain. `seq` is global; `prev` links within the restaurant."""
    rid = store.current.get()
    with store._lock:
        last = store.one("select hash from ledger where restaurant_id=? order by seq desc limit 1", rid)
        top = store.one("select max(seq) seq from ledger")["seq"] or 0
        e = {"seq": top + 1, "restaurant_id": rid, "ts": store.now(), "actor": actor, "kind": kind, "ref": ref,
             "body": body, "prev": last["hash"] if last else GENESIS}
        e["hash"] = crypto.sha256(_entry_bytes(e))
        e["signer"], e["sig"] = signer, crypto.sign(signer, e["hash"].encode())
        store.insert("ledger", **{**e, "body": json.dumps(body, ensure_ascii=False)})
    events.emit("ledger", seq=e["seq"], actor=actor, entry_kind=kind, ref=ref, hash=e["hash"])
    return e


def entries(rid, ref=None):
    rows = store.q("select * from ledger where restaurant_id=?" + (" and ref=?" if ref else "") + " order by seq",
                   rid, *([ref] if ref else []))
    for r in rows:
        r["body"] = json.loads(r["body"])
    return rows


def verify(rid):
    """Recomputes the chain. Returns every entry with `ok` and, for a broken one, the reason."""
    out, prev, broken = [], GENESIS, False
    cosigns = {c["seq"]: c for c in store.q("select * from ledger_cosign")}
    for e in entries(rid):
        problem = None
        if e["prev"] != prev:
            problem = "previous-hash link broken"
        elif crypto.sha256(_entry_bytes(e)) != e["hash"]:
            problem = "contents changed after signing"
        elif not crypto.verify_service(e["signer"], e["hash"].encode(), e["sig"]):
            problem = "signature invalid"
        broken = broken or problem is not None
        out.append({**e, "ok": not broken, "problem": problem, "cosigned": e["seq"] in cosigns})
        prev = e["hash"]
    return out


def head(rid):
    return store.one("select seq, hash from ledger where restaurant_id=? order by seq desc limit 1", rid)
