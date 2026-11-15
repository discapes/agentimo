"""Checks an Agentimo proof bundle offline, trusting nothing but the public keys inside it and the maths.

    curl -s https://aimo.jc.iffe.eu/api/orders/A-123ABC/proof > proof.json
    python verify_proof.py proof.json
"""
import base64
import hashlib
import json
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def unb64(s):
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def device_ok(jwk, data, sig):
    key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(jwk["x"])), int.from_bytes(unb64(jwk["y"])), ec.SECP256R1()).public_key()
    raw = unb64(sig)
    try:
        key.verify(encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:])), data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def service_ok(pub, data, sig):
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(unb64(pub)).verify(unb64(sig), data)
        return True
    except InvalidSignature:
        return False


def check(b):
    results = []
    say = lambda ok, text: results.append((ok, text))
    for kind, items in (("instruction", b["instructions"]), ("standing rule", b["standing_rules"])):
        for u in items:
            env = json.loads(u["envelope"])
            say(device_ok(u["device_jwk"], u["envelope"].encode(), u["signature"]),
                f"{kind} {u['id']} signed on the chef's phone at {env['ts']}")
            if u["audio"]:
                say(hashlib.sha256(unb64(u["audio"])).hexdigest() == env["audio_sha256"],
                    f"{kind} {u['id']}: the recording is the one that was signed")
    o = b["order"]
    lines = [l for l in o["lines"] if not l.get("removed")]
    total = round(sum(l["line_total"] or 0 for l in lines), 2)
    order_hash = hashlib.sha256(canonical({"id": o["id"], "delivery_date": o["delivery_date"], "total": total,
                                           "lines": [{"product_id": l["product_id"], "quantity": l["quantity"]} for l in lines]})).hexdigest()
    say(order_hash == b["order_hash"], f"order {o['id']}: {len(lines)} lines, €{total:,.2f}, hash {order_hash[:16]}…")
    for e in b["ledger"]:
        entry = {k: e[k] for k in ("seq", "restaurant_id", "ts", "actor", "kind", "ref", "body", "prev")}
        intact = hashlib.sha256(canonical(entry)).hexdigest() == e["hash"]
        say(intact and service_ok(b["service_keys"][e["signer"]], e["hash"].encode(), e["sig"]),
            f"ledger #{e['seq']} {e['kind']} by {e['actor']}, signed by the {e['signer']} key")
        if e["kind"] == "order.approved":
            body = e["body"]
            say(body["order_hash"] == order_hash, "the approval covers exactly these lines and this total")
            if "instruction" in body:
                say(device_ok(b["devices"][body["device"]], body["envelope"].encode(), body["signature"]),
                    f"the chef approved by signed words ({body['instruction']})")
            else:
                say(device_ok(b["devices"][body["device"]], order_hash.encode(), body["signature"]),
                    "the chef's device signed the order hash")
    return results


if __name__ == "__main__":
    results = check(json.load(open(sys.argv[1])))
    for ok, text in results:
        print(("✓ " if ok else "✗ ") + text)
    sys.exit(0 if all(ok for ok, _ in results) else 1)
