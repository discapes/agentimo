"""HTTP entry point: the chef's phone app, the control room, the agents' MCP and REST interfaces."""
import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import agent
import attacks
import catalog
import crypto
import events
import gateway
import instructions
import ledger
import mcp_server
import seed
import store

HERE = Path(__file__).parent

mcp_app = mcp_server.mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app):
    events.loop = asyncio.get_running_loop()
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="Valio Aimo agent gateway", lifespan=lifespan,
              description="Order interface for purchasing agents. Orders are verified against the chef's signed "
                          "instructions; the same operations are available over MCP at /mcp.")
app.mount("/mcp", mcp_app)

# Every browser is its own restaurant. The server sets this cookie on the first page load, before the laptop
# page's two frames (app and control room) make their own requests, so both always share one session.
COOKIE = "aimo_r"
UNDO_SECONDS = 60
SESSION_PATHS = ("/api/", "/events")


@app.middleware("http")
async def session(request: Request, call_next):
    path = request.url.path
    if not (path in ("/", "/app", "/room") or path.startswith(SESSION_PATHS)):
        return await call_next(request)
    rid = request.cookies.get(COOKIE, "")
    new = not re.fullmatch(r"r-[0-9a-f]{16}", rid)
    if new:
        rid = "r-" + secrets.token_hex(8)
    store.current.set(rid)
    if path.startswith(SESSION_PATHS) and rid not in seed._ready:
        await asyncio.to_thread(seed.ensure, rid)
    response = await call_next(request)
    if new:
        response.set_cookie(COOKIE, rid, max_age=365 * 86400, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return response


def rid():
    return store.current.get()


@app.post("/api/session/new", include_in_schema=False)
def new_session(request: Request):
    """Starts this browser over as a new restaurant. The old session stays in the database untouched."""
    from fastapi.responses import JSONResponse
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, "r-" + secrets.token_hex(8), max_age=365 * 86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


class NoCache(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", NoCache(directory=HERE / "static"), name="static")


def page(name):
    return FileResponse(HERE / "static" / name, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def home():
    """Laptop: the app beside the control room. Phone: the app alone (the page redirects to /app)."""
    return page("index.html")


@app.get("/app", include_in_schema=False)
def chef_page():
    return page("chef.html")


@app.get("/room", include_in_schema=False)
def room_page():
    return page("room.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(HERE / "static/sw.js", headers={"Cache-Control": "no-cache"}, media_type="text/javascript")


@app.get("/events", include_in_schema=False)
async def event_stream(request: Request):
    since = int(request.headers.get("last-event-id") or 0)
    return StreamingResponse(events.stream(rid(), since), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Enrollment: each browser that opens the app gets its own signing key, created on first open.

@app.post("/api/enroll", include_in_schema=False)
def enroll(body: dict):
    jwk = body["jwk"]
    did = crypto.device_fingerprint(jwk)
    if not store.one("select 1 from devices where id=?", did):
        store.insert("devices", id=did, restaurant_id=rid(), kind="device", public_jwk=jwk, created=store.now())
        ledger.append("chef", "device.enrolled", did, {"device": did, "jwk": jwk})
    return {"device": did, "restaurant": rid()}


@app.get("/api/me", include_in_schema=False)
def me(device: str = ""):
    r = store.restaurant(rid())
    devices = store.q("select id, kind, created from devices where restaurant_id=?", rid())
    import verifier
    rules = verifier.standing_rules(rid())
    return {"restaurant": rid(), "name": r["name"], "profile": r["profile"], "devices": devices,
            "enrolled": any(d["id"] == device for d in devices),
            "rules": [{"id": u["id"], "text": u["text"], "limits": json.loads(u["envelope"]).get("limits")} for u in rules],
            "defaults": {"rules": seed.RULES, "limits": seed.LIMITS}}


# Chef's messages and orders

def _utterance_view(u):
    return u and {"id": u["id"], "kind": u["kind"], "text": u["text"], "words": json.loads(u["words"] or "null"),
                  "audio": bool(u["audio_path"]), "device": u["device_id"], "created": u["created"],
                  "envelope": json.loads(u["envelope"]), "signature": u["signature"]}


def _order(oid):
    """The order if it belongs to this session's restaurant."""
    o = gateway.get_order(oid)
    return o if o and o["restaurant_id"] == rid() else None


def order_view(o):
    o["approved"] = next((e["body"] for e in ledger.entries(rid(), o["id"]) if e["kind"] == "order.approved"), None)
    if o["status"] == "placed" and not o["approved"]:
        o["undo_until"] = datetime.fromisoformat(o["updated"]).timestamp() + UNDO_SECONDS
    return o


@app.get("/api/chat", include_in_schema=False)
def chat():
    msgs = store.q("select * from messages where restaurant_id=? order by id desc limit 40", rid())[::-1]
    for m in msgs:
        m["utterance"] = _utterance_view(store.one("select * from utterances where id=?", m["utterance_id"])) \
            if m["utterance_id"] else None
    return msgs


def _message(role, text=None, utterance_id=None, order_id=None):
    store.insert("messages", restaurant_id=rid(), role=role, text=text, utterance_id=utterance_id, order_id=order_id,
                 created=store.now())
    events.emit("chat", role=role)


def _approve_by_words(u):
    """If the agent's last message presented an order that is waiting, and the chef's new words approve exactly that
    order and ask for nothing else, approve it with those signed words."""
    last = store.one("select order_id from messages where restaurant_id=? and role='assistant' order by id desc limit 1",
                     rid())
    o = gateway.get_order(last["order_id"]) if last and last["order_id"] else None
    import verifier
    if not o or o["status"] != "held" or not verifier.approves(u, o):
        return None
    store.run("update utterances set consumed_by=? where id=?", o["id"], u["id"])
    ledger.append("chef", "order.approved", o["id"], {"order_hash": o["hash"], "total_eur": o["total"],
                  "device": u["device_id"], "instruction": u["id"], "envelope": u["envelope"], "signature": u["signature"]})
    gateway.place(o, "chef")
    return o["id"]


async def _handle(u):
    try:
        approved = await asyncio.to_thread(_approve_by_words, u)
        if approved:
            return _message("assistant", "Approved. It's on its way to Valio Aimo.")
        reply, order_ids = await agent.run(rid(), u)
    except Exception as e:  # the chef sees a plain failure; the room shows the cause
        events.emit("agent.error", error=str(e)[:300])
        reply, order_ids = "Something went wrong on my side. Could you try again?", []
    # The reply carries the first order's card; any further orders from the same message get their own cards.
    _message("assistant", reply, order_id=order_ids[0] if order_ids else None)
    for oid in order_ids[1:]:
        _message("assistant", "", order_id=oid)


VOICE = "EXAVITQu4vr4xnSDxMaL"  # ElevenLabs "Sarah", the voice of the pool video


@app.get("/api/messages/{mid}/voice", include_in_schema=False)
def message_voice(mid: int):
    """The assistant's reply spoken, generated once and cached."""
    path = store.DATA / "voice" / f"{mid}.mp3"
    if not path.exists():
        m = store.one("select text from messages where id=? and role='assistant'", mid)
        if not m or not m["text"]:
            raise HTTPException(404)
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128", timeout=30,
                       headers={"xi-api-key": instructions.ELEVEN_KEY},
                       json={"text": m["text"], "model_id": "eleven_v4", "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}})
        r.raise_for_status()
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(r.content)
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/utterances", include_in_schema=False)
async def post_utterance(envelope: str = Form(), signature: str = Form(), audio: UploadFile | None = File(None)):
    if json.loads(envelope).get("restaurant") != rid():
        raise HTTPException(403, "signed for another session")
    data = await audio.read() if audio else None
    try:
        u = await asyncio.to_thread(instructions.receive, envelope, signature, data)
    except instructions.Refused as e:
        raise HTTPException(403, str(e))
    if u["kind"] == "instruction":
        _message("chef", utterance_id=u["id"])
        asyncio.create_task(_handle(u))
    return _utterance_view(u)


@app.get("/api/orders/{oid}", include_in_schema=False)
def order(oid: str):
    o = _order(oid)
    if not o:
        raise HTTPException(404)
    return order_view(o)


@app.get("/api/orders", include_in_schema=False)
def orders():
    """This session's recent orders, each with its history in plain words. The hash chain is verified once."""
    chain = ledger.verify(rid())
    out = []
    for r in store.q("select id from orders where restaurant_id=? order by created desc limit 30", rid()):
        o = gateway.get_order(r["id"])
        steps = _steps(o, chain)
        out.append({"id": o["id"], "status": o["status"], "total": o["total"], "agent": o["agent"],
                    "created": o["created"], "delivery_date": o["delivery_date"], "lines": len(o["lines"]),
                    "signed": bool(o["instruction_ids"]), "steps": steps, "intact": all(st["ok"] for st in steps)})
    return out


@app.get("/api/orders/{oid}/proof")
def proof(oid: str):
    """Everything needed to check an order without trusting this server: the chef's signed instructions with
    their recordings, the standing rules, the ledger entries with their signatures, the approval and the
    public keys. `verify_proof.py` checks a bundle offline."""
    o = _order(oid)
    if not o:
        raise HTTPException(404)
    def utt(u):
        dev = store.one("select public_jwk from devices where id=?", u["device_id"])
        return {"id": u["id"], "envelope": u["envelope"], "signature": u["signature"],
                "device_jwk": json.loads(dev["public_jwk"]) if dev else None, "transcript": u["text"],
                "audio": crypto.b64(Path(u["audio_path"]).read_bytes()) if u["audio_path"] else None}
    instrs = [utt(store.one("select * from utterances where id=?", i)) for i in o["instruction_ids"]]
    import verifier
    devices = {d["id"]: json.loads(d["public_jwk"]) for d in store.q("select * from devices where restaurant_id=?", rid())}
    entries = ledger.entries(rid(), oid)
    return {"order": {k: o[k] for k in ("id", "delivery_date", "status", "total")} |
                     {"lines": [{k: l.get(k) for k in ("product_id", "name", "quantity", "sales_unit", "line_total", "removed")}
                                for l in o["lines"]]},
            "order_hash": o["hash"], "instructions": instrs,
            "standing_rules": [utt(r) for r in verifier.standing_rules(rid())],
            "ledger": [{k: e[k] for k in ("seq", "restaurant_id", "ts", "actor", "kind", "ref", "body", "prev", "hash", "signer", "sig")}
                       for e in entries],
            "devices": devices,
            "service_keys": {n: crypto.public_key(n) for n in ("gateway", "verifier", "agent")}}


DECISION = {"placed": "placed it", "held": "held it for the chef", "returned": "sent it back to the agent",
            "rejected": "refused it"}


def _steps(o, chain):
    """The order's story in plain words from its ledger entries; `ok` is false for an entry changed after signing."""
    refs = {o["id"], *o["instruction_ids"]}
    steps = []
    for e in (e for e in chain if e["ref"] in refs):
        b, k = e["body"], e["kind"]
        if k == "instruction.signed":
            text = f"Chef said “{b.get('transcript') or b['envelope'].get('text')}”"
        elif k == "order.submitted":
            n = len(b["lines"])
            text = f"{'Outside agent' if b['agent'] == 'external-agent' else 'Ordering agent'} submitted {n} line{'s' * (n != 1)}"
        elif k == "order.verdict":
            reasons = [r for r in b["reasons"] if not r.startswith("Line ")] or b["reasons"]
            text = f"Verifier {DECISION.get(b['decision'], b['decision'])}" + (f": {reasons[0]}" if reasons else "")
        elif k == "order.line_removed":
            text = f"Chef removed line {b['line']}"
        elif k == "order.approved":
            text = "Chef approved by saying so" if "instruction" in b else "Chef approved with a tap"
        elif k == "order.placed":
            text = f"Sent to Valio Aimo, €{b['total_eur']:,.2f}"
        else:
            text = {"order.declined": "Chef declined",
                    "order.cancelled": "Cancelled at the chef's request" if "instruction" in b else "Chef undid the order"}.get(k, k)
        steps.append({"ts": e["ts"], "text": text, "ok": e["problem"] is None,
                      "instruction": e["ref"] if k == "instruction.signed" else None})
    return steps


@app.post("/api/orders/{oid}/lines/{i}/remove", include_in_schema=False)
def remove_line(oid: str, i: int):
    o = _order(oid)
    if not o or o["status"] != "held":
        raise HTTPException(409, "order is not waiting for approval")
    o["lines"][i]["removed"] = True
    gateway.save_order(o)
    ledger.append("chef", "order.line_removed", oid, {"line": i + 1, "product_id": o["lines"][i]["product_id"]})
    return order_view(_order(oid))


@app.post("/api/orders/{oid}/approve", include_in_schema=False)
def approve(oid: str, body: dict):
    """The chef's device signs the order's hash, so the approval covers exactly these lines and this total."""
    o = _order(oid)
    if not o or o["status"] != "held":
        raise HTTPException(409, "order is not waiting for approval")
    if body.get("hash") != o["hash"]:
        raise HTTPException(409, "the order changed; look at it again")
    dev = store.one("select * from devices where id=? and restaurant_id=?", body.get("device"), rid())
    if not dev or not crypto.verify_device(json.loads(dev["public_jwk"]), o["hash"].encode(), body.get("signature", "")):
        raise HTTPException(403, "approval does not match this order")
    ledger.append("chef", "order.approved", oid, {"order_hash": o["hash"], "total_eur": o["total"],
                                                  "device": dev["id"], "signature": body["signature"]})
    gateway.place(o, "chef")
    return order_view(_order(oid))


@app.post("/api/orders/{oid}/undo", include_in_schema=False)
def undo(oid: str):
    """An order Agentimo placed on its own can be undone for a minute."""
    o = _order(oid)
    if not o or o["status"] != "placed" or any(e["kind"] == "order.approved" for e in ledger.entries(rid(), oid)):
        raise HTTPException(409, "this order can't be undone")
    if (datetime.now().astimezone() - datetime.fromisoformat(o["updated"])).total_seconds() > UNDO_SECONDS + 5:
        raise HTTPException(409, "too late to undo")
    gateway.save_order(o, status="cancelled")
    ledger.append("chef", "order.cancelled", oid, {"order_hash": o["hash"]})
    return order_view(_order(oid))


@app.post("/api/orders/{oid}/decline", include_in_schema=False)
def decline(oid: str):
    o = _order(oid)
    if not o or o["status"] != "held":
        raise HTTPException(409, "order is not waiting for approval")
    gateway.save_order(o, status="declined")
    ledger.append("chef", "order.declined", oid, {"order_hash": o["hash"]})
    return order_view(_order(oid))


# Ledger

@app.get("/api/ledger", include_in_schema=False)
def ledger_view():
    return {"entries": ledger.verify(rid()), "keys": {n: crypto.public_key(n) for n in ("gateway", "verifier", "agent")}}


@app.post("/api/ledger/cosign", include_in_schema=False)
def cosign(body: dict):
    """The phone signs the ledger head it has seen, so the gateway can't rewrite history without the chef."""
    head = ledger.head(rid())
    dev = store.one("select * from devices where id=? and kind='device'", body.get("device"))
    if not head or not dev or body.get("hash") != head["hash"]:
        raise HTTPException(409, "not the current head")
    if not crypto.verify_device(json.loads(dev["public_jwk"]), head["hash"].encode(), body["signature"]):
        raise HTTPException(403, "bad signature")
    store.insert("ledger_cosign", seq=head["seq"], hash=head["hash"], device_id=dev["id"], sig=body["signature"],
                 ts=store.now())
    events.emit("ledger.cosigned", seq=head["seq"])
    return {"seq": head["seq"]}


@app.get("/api/ledger/head", include_in_schema=False)
def ledger_head():
    return ledger.head(rid()) or {}


# Control room demos

@app.post("/api/demo/{name}", include_in_schema=False)
async def demo(name: str, body: dict | None = None):
    if name not in attacks.ALL:
        raise HTTPException(404)
    return await attacks.run(name)


# REST for agents, the same operations as the MCP tools

def _agent(authorization):
    who = mcp_server.who((authorization or "").removeprefix("Bearer ").strip())
    if not who:
        raise HTTPException(401, "unknown token")
    store.current.set(who["restaurant"])
    return who


@app.get("/v1/offers", tags=["agents"])
def rest_offers(query: str, delivery_date: str, quantity: float | None = None, unit: str | None = None,
                free_from: str = "", authorization: str = Header(None)):
    """Offers for a need with live price and availability. `free_from` is comma-separated."""
    who = _agent(authorization)
    return gateway.find_products(who["restaurant"], query, delivery_date, quantity, unit,
                                 [f for f in free_from.split(",") if f])


@app.get("/v1/products/{pid}/substitutes", tags=["agents"])
def rest_substitutes(pid: str, delivery_date: str, quantity: float | None = None, unit: str | None = None,
                     authorization: str = Header(None)):
    who = _agent(authorization)
    return gateway.substitutes(who["restaurant"], pid, delivery_date, quantity, unit)


@app.get("/v1/history", tags=["agents"])
def rest_history(authorization: str = Header(None)):
    return gateway.order_history(_agent(authorization)["restaurant"])


@app.post("/v1/orders", tags=["agents"])
def rest_submit(body: dict, authorization: str = Header(None)):
    """Body: `{"delivery_date", "lines": [{"product_id", "quantity", "cites": [{"instruction_id", "quote"}]}]}`."""
    who = _agent(authorization)
    return gateway.submit_order(who["restaurant"], who["agent"], body["delivery_date"], body["lines"], body.get("note"))


@app.get("/v1/orders/{oid}", tags=["agents"])
def rest_order(oid: str, authorization: str = Header(None)):
    who = _agent(authorization)
    o = _order(oid)
    if not o or o["restaurant_id"] != who["restaurant"]:
        raise HTTPException(404)
    return gateway.order_summary(o)
