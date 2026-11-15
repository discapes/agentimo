"""Decides whether an order may go through. It reads only what the chef signed (the recording itself, or typed
text), the chef's signed standing rules and the order; never the agent's conversation or the transcript.

Stages: 1. signatures, 2. checks in code against live Valio data and history, 3. one Gemini Decisions request
that listens to the signed recordings, 4. the chef's autonomy setting turns the results into a decision."""
import base64
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

import catalog
import crypto
import events
import gateway
import ledger
import store
import valio_api

GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or next(
    (l.split("=", 1)[1].strip().strip("'\"") for l in (Path.home() / ".config/junction/gemini.env").read_text().splitlines()
     if l.startswith("GEMINI_API_KEY=")), None)
# Boundaries calibrated on 61 labelled orders: "unclear" only between NO and UNSURE, rules flagged below RULES,
# an order held for something missing above MISSING.
YES, NO, UNSURE, RULES, MISSING, APPROVE = 0.7, 0.3, 0.45, 0.5, 0.8, 0.8
MAX_AGE_H = 24
CONTEXT_H = 6  # the chef's other signed messages from this long ago are context for the Decisions check
PRICE_PER_TOKEN = 0.10 / 1e6
DEFAULT_LIMITS = {"auto_eur": 2500, "max_eur": 10000, "autonomy": "deviations"}


def _long_date(d):
    return datetime.fromisoformat(d).strftime("%A %-d %B %Y")


def _ratio(x):
    return round(x) if x >= 10 else round(x, 1)


def _day(order_id):
    o = store.one("select delivery_date from orders where id=?", order_id)
    return o and o["delivery_date"]


def utterance_problem(u, rid, order_id=None):
    """Why a signed utterance can't back an order, or None. Checks the device signature over the exact envelope
    bytes and that the stored recording is the one whose hash was signed."""
    if not u:
        return "instruction not found"
    dev = store.one("select * from devices where id=? and kind='device'", u["device_id"])
    if not dev or dev["restaurant_id"] != rid:
        return "signed by a key not enrolled for this restaurant"
    if not crypto.verify_device(json.loads(dev["public_jwk"]), u["envelope"].encode(), u["signature"]):
        return "signature does not match"
    env = json.loads(u["envelope"])
    if env.get("restaurant") != rid:
        return "signed for another restaurant"
    if u["audio_path"] and crypto.sha256(Path(u["audio_path"]).read_bytes()) != env.get("audio_sha256"):
        return "recording differs from the signed one"
    if not u["audio_path"] and env.get("text") != u["text"]:
        return "text differs from the signed one"
    if u["revoked"]:
        return "revoked"
    if u["kind"] == "instruction":
        # One message may back orders for several days; backing a second order for the same day is a replay.
        if u["consumed_by"] and u["consumed_by"] != order_id and (not order_id or _day(u["consumed_by"]) == _day(order_id)):
            return f"already used for order {u['consumed_by']} (replay)"
        if (datetime.now().astimezone() - datetime.fromisoformat(env["ts"])).total_seconds() > MAX_AGE_H * 3600:
            return f"older than {MAX_AGE_H} h"
    return None


def standing_rules(rid):
    rules = store.q("select * from utterances where restaurant_id=? and kind='rule' and revoked=0 order by created", rid)
    return [r for r in rules if utterance_problem(r, rid) is None]


def limits(rules):
    """Spending limits and autonomy come from the newest signed rules that set them."""
    out = dict(DEFAULT_LIMITS)
    for r in rules:
        out |= json.loads(r["envelope"]).get("limits") or {}
    return out


def _decide(blocks, questions):
    t = time.time()
    r = httpx.post("https://generativelanguage.googleapis.com/v1beta/decisions", timeout=30,
                   headers={"x-goog-api-key": GEMINI_KEY},
                   json={"model": "gemini-decision-1", "input": blocks, "questions": questions})
    r.raise_for_status()
    body = r.json()
    return body["answers"], body["usage"]["total_input_tokens"], time.time() - t


def _about_order(u, o, questions):
    """Gemini Decisions yes/no probabilities for questions about the chef's new words and an existing order."""
    lines = "; ".join(f"{l['quantity']:g} × {l['name']}" for l in o["lines"] if not l.get("removed"))
    blocks = [{"type": "text", "text": f"A restaurant chef has an order ({o['status']}): {lines}, total "
               f"€{o['total']:,.2f}, delivery {_long_date(o['delivery_date'])}. The chef then said:\n"}]
    if u["audio_path"]:
        blocks.append({"type": "audio", "mime_type": "audio/ogg",
                       "data": base64.b64encode(Path(u["audio_path"]).with_suffix(".ogg").read_bytes()).decode()})
    else:
        blocks.append({"type": "text", "text": f'"{u["text"]}"'})
    answers, _, secs = _decide(blocks, {k: {"type": "noul", "instructions": q} for k, q in questions.items()})
    p = {k: a["noul"] for k, a in answers.items()}
    events.emit("verifier", order_id=o["id"], stage="about_order", answers=p, ms=round(1000 * secs))
    return p


def asks_to_cancel(u, o):
    """Probability that the chef's new words ask to cancel this order or to change it (amount, date, items)."""
    return _about_order(u, o, {"cancel": "Does the chef ask to cancel this order, or to change its amounts, items or "
                                         "delivery day so that it needs replacing?"})["cancel"]


def approves(u, o):
    """Whether the chef's new words approve the waiting order as it is, with nothing else asked for."""
    p = _about_order(u, o, {
        "approve": "Does the chef approve the waiting order and want it sent, for example 'yes, go ahead' or "
                   "'approve it'? A new or different request is not an approval.",
        "more": "Besides approving, does the chef ask for anything else: new items, other amounts or another day?"})
    return p["approve"] >= APPROVE and p["more"] < 0.5


UNIT_WORDS = {"ST": "single packs", "KI": "cases", "KG": "kg", "PAK": "packs", "RAS": "boxes", "BAG": "bags",
              "PRK": "tubs", "BOT": "bottles", "SK": "sacks", "CS": "cases", "KLE": "kegs", "KRT": "cartons",
              "KAN": "cans", "PAL": "pallets"}


def _units(l, f):
    """An order line's amount in words, e.g. "10 single packs of 1 l = 10 l"."""
    unit = UNIT_WORDS.get(l["sales_unit"], l["sales_unit"].lower())
    if unit == "kg" or not f.get("measure"):
        return f"{l['quantity']:g} {unit}"
    each = f["measure"] / l["quantity"]
    return f"{l['quantity']:g} {unit} of {each:g} {f['unit']} = {f['measure']:g} {f['unit']}"


def _decision_input(o, instructions, rules, facts, context=(), earlier=()):
    blocks = [{"type": "text", "text": f"Today is {_long_date(date.today().isoformat())}. A restaurant chef gave "
               "the instructions below to an AI ordering assistant, which then drafted the order further below.\n"}]
    cited = {u["id"] for u in instructions}
    for n, u in enumerate(sorted([*context, *instructions], key=lambda u: u["created"]), 1):
        label = f"Chef's instruction I{n}" if u["id"] in cited else f"Earlier message M{n} from the chef, for context"
        if u["audio_path"]:
            blocks.append({"type": "text", "text": f"\n{label}, voice recording:\n"})
            blocks.append({"type": "audio", "mime_type": "audio/ogg",
                           "data": base64.b64encode(Path(u["audio_path"]).with_suffix(".ogg").read_bytes()).decode()})
        else:
            blocks.append({"type": "text", "text": f"\n{label}, typed: \"{u['text']}\"\n"})
    if rules:
        blocks.append({"type": "text", "text": "\nChef's standing rules:\n" +
                       "".join(f"- {r['text']}\n" for r in rules)})
    lines = "".join(f"Line {i + 1}: {l['name']}" + (f" (Finnish name: {fi})" if (fi := catalog.get(l["product_id"])["name_fi"]) else "")
                    + f", {_units(l, f)}."
                    + (f" From the order history: the restaurant usually orders {f['usual']:g} × {l['sales_unit']} of this product "
                       f"per week, so this line is {f['ratio']:g} times the usual." if f.get("usual") else
                       f" From the order history: {f['ratio']:g} times what the restaurant usually orders of this kind of "
                       "product per week." if f.get("ratio") else "")
                    + (f" Already ordered for the same delivery day in earlier orders: {f['already']:g} {UNIT_WORDS.get(l['sales_unit'], l['sales_unit'].lower())}."
                       if f.get("already") else "")
                    + "\n" for i, (l, f) in enumerate(zip(o["lines"], facts)) if not l.get("removed"))
    if earlier:
        blocks.append({"type": "text", "text": "\nOrders already made earlier in this conversation:\n" + "".join(
            f"- {', '.join(x['name'] for x in json.loads(e['lines']) if not x.get('removed'))} "
            f"({e['status']}, delivery {_long_date(e['delivery_date'])})\n" for e in earlier)})
    blocks.append({"type": "text", "text": f"\nDraft order, delivery {_long_date(o['delivery_date'])}:\n{lines}"})
    if o.get("note"):
        blocks.append({"type": "text", "text": f"\nThe assistant's note: {o['note']}\n"})
    return blocks


def _questions(o, facts, rules):
    q = {}
    for i, (l, f) in enumerate(zip(o["lines"], facts), 1):
        if l.get("removed"):
            continue
        q[f"L{i}_asked"] = {"type": "noul", "instructions": f"Do the chef's instructions, in whatever language, ask "
                            f"for the kind of product in order line {i}? Brand, pack size, fat content and other details "
                            "the chef did not mention do not matter."}
        q[f"L{i}_amount"] = {"type": "noul", "instructions": f"Does the total amount in order line {i} match the amount "
                             "the chef asks for? If the chef asks for the usual amount, compare with the usual weekly amount from the order "
                             "history. If the line adds to an amount already ordered, compare the combined amount. If the "
                             "chef names no amount, answer yes. Rounding up to whole packs counts as a match, for example 6 kg for 5 kg "
                             "sold in 2 kg packs."}
        if f.get("ratio") and f["ratio"] >= 3:
            q[f"L{i}_reason"] = {"type": "noul", "instructions": "Does the chef mention an event, occasion or other "
                                 f"reason that explains needing much more than usual of the product in order line {i}?"}
        if rules:
            q[f"L{i}_rules"] = {"type": "noul", "instructions": f"Is order line {i} consistent with every one of the "
                                "chef's standing rules? A line no rule is about counts as consistent, and so does a line the chef explicitly "
                                "agreed to in their instructions."}
    q["missing"] = {"type": "noul", "instructions": "Do the chef's instructions ask for any product that neither this "
                    "draft order nor an order made earlier in the conversation covers, and that the assistant's note does not say it "
                    "is asking the chef about separately?"}
    q["date"] = {"type": "noul", "instructions": f"Is delivery on {_long_date(o['delivery_date'])} consistent with "
                 "when the chef wants the delivery? If the chef names no day, answer yes."}
    return q


def verify(o):
    t0 = time.time()
    rid = o["restaurant_id"]
    reasons, signature_problems, timing = [], [], {}
    flags = [[] for _ in o["lines"]]
    stage = lambda name, **kw: events.emit("verifier", order_id=o["id"], stage=name, **kw)

    # 1. Signatures
    # An instruction already used by another order may back a correction ("make that 30") together with a new
    # one; an order citing only used instructions is a replay.
    instructions, used = [], []
    for iid in o["instruction_ids"]:
        u = store.one("select * from utterances where id=? and kind='instruction'", iid)
        problem = utterance_problem(u, rid, o["id"])
        if problem and problem.startswith("already used"):
            used.append((u, f"Instruction {iid}: {problem}"))
        elif problem:
            signature_problems.append(f"Instruction {iid}: {problem}")
        else:
            instructions.append(u)
    if used and not instructions:
        signature_problems += [p for _, p in used]
    else:
        instructions += [u for u, _ in used]
    # The chef's other signed messages from the last hours give the cited ones their context ("make it 30").
    context = []
    if instructions:
        since = (datetime.now().astimezone() - timedelta(hours=CONTEXT_H)).isoformat(timespec="seconds")
        for u in store.q("select * from utterances where restaurant_id=? and kind='instruction' and created >= ? "
                         "order by created", rid, since):
            problem = utterance_problem(u, rid, o["id"])
            if u["id"] not in o["instruction_ids"] and (problem is None or problem.startswith("already used")):
                context.append(u)
    earlier = store.q("select * from orders where restaurant_id=? and id<>? and created >= ? and status in ('placed', 'held') "
                      "order by created", rid, o["id"],
                      (datetime.now().astimezone() - timedelta(hours=CONTEXT_H)).isoformat(timespec="seconds"))
    rules = standing_rules(rid)
    lim = limits(rules)
    timing["signatures_ms"] = round(1000 * (time.time() - t0))
    stage("signatures", ok=not signature_problems, instructions=len(instructions), rules=len(rules),
          problems=signature_problems, ms=timing["signatures_ms"])
    if signature_problems:
        return _finish(o, "rejected", signature_problems, flags, timing, {}, 0, lim)
    if not instructions:
        reasons.append("No signed instruction from the chef backs this order")
        for f in flags:
            f.append({"kind": "unsigned", "text": "No signed instruction from the chef"})

    # 2. Checks in code: fresh price and availability, history, limits
    t = time.time()
    hist = gateway.usual(rid)
    pids = [l["product_id"] for l in o["lines"]]
    quotes = valio_api.quote(pids + [p for p in hist if p not in pids], o["delivery_date"])
    facts, returned = [], []
    for i, l in enumerate(o["lines"]):
        q = quotes[l["product_id"]]
        cat = catalog.get(l["product_id"])["category"]
        mps = q["measure_per_sales_unit"]
        f = {"measure": round(l["quantity"] * mps, 2) if mps else None, "unit": (q["unit_measure"] or "").lower()}
        l.update(price=q["price"], line_total=round(l["quantity"] * q["price"], 2) if q["price"] else None,
                 measure=f["measure"], unit_measure=f["unit"], closing_time=q["closing_time"])
        problem = gateway.orderability(q, l["quantity"], o["delivery_date"])
        if problem and not l.get("removed"):
            returned.append(f"Line {i + 1} ({l['name']}): {problem}")
            flags[i].append({"kind": "availability", "text": problem.capitalize()})
        # Usual weekly amount of this product, or of the same category measured in litres or kilograms.
        if l["product_id"] in hist:
            f["usual"] = hist[l["product_id"]]["weekly"]
            f["ratio"] = _ratio(l["quantity"] / f["usual"])
        else:
            # Usual products of the same kind: same category, or the same category group with a similar name
            # (fresh and frozen salmon fillets sit in different categories).
            comparable = {p: h for p, h in hist.items()
                          if quotes[p]["unit_measure"] == q["unit_measure"] and quotes[p]["measure_per_sales_unit"]}
            same = [(p, h) for p, h in comparable.items() if catalog.get(p)["category"] == cat] or sorted(
                ((p, h) for p, h in comparable.items() if catalog.get(p)["category"][:2] == cat[:2]
                 and gateway._similarity(catalog.get(p)["name"], l["name"]) >= 0.2),
                key=lambda ph: -gateway._similarity(catalog.get(ph[0])["name"], l["name"]))[:1]
            if same and f["measure"]:
                usual_measure = sum(h["weekly"] * quotes[p]["measure_per_sales_unit"] for p, h in same)
                f["ratio"] = _ratio(f["measure"] / usual_measure)
                f["substitutes_for"] = [catalog.get(p)["name"] for p, _ in same]
                l["instead_of"] = f["substitutes_for"][0]
        f["already"] = sum(x["quantity"] for r in store.q(
            "select lines from orders where restaurant_id=? and id<>? and delivery_date=? and status in ('placed', 'held')",
            rid, o["id"], o["delivery_date"]) for x in json.loads(r["lines"])
            if x["product_id"] == l["product_id"] and not x.get("removed"))
        if f.get("ratio") and f["ratio"] >= 3 and not l.get("removed"):
            l["ratio"] = f["ratio"]
        facts.append(f)
    l_total = gateway.total(o)
    if l_total > lim["max_eur"]:
        returned.append(f"Total €{l_total:,.2f} is over the €{lim['max_eur']:,} order maximum")
    elif l_total > lim["auto_eur"]:
        reasons.append(f"€{l_total:,.2f} is more than the €{lim['auto_eur']:,} Agentimo may spend without the chef")
    timing["rules_ms"] = round(1000 * (time.time() - t))
    stage("rules", ok=not returned, total=l_total, limits=lim, returned=returned,
          ratios=[f.get("ratio") for f in facts], ms=timing["rules_ms"])
    if returned:
        return _finish(o, "returned", returned, flags, timing, {}, 0, lim)

    # 3. Gemini Decisions listens to the signed recordings
    answers, tokens = {}, 0
    if instructions:
        stage("decisions.start", questions=len(_questions(o, facts, rules)))
        answers, tokens, secs = _decide(_decision_input(o, instructions, rules, facts, context, earlier), _questions(o, facts, rules))
        timing["decisions_ms"] = round(1000 * secs)
        p = {k: a["noul"] for k, a in answers.items()}
        for i, l in enumerate(o["lines"], 1):
            if l.get("removed"):
                continue
            fl = flags[i - 1]
            asked = p.get(f"L{i}_asked", 1)
            if asked < NO:
                fl.append({"kind": "not_asked", "text": "You didn't ask for this", "p": asked})
            elif asked < UNSURE:
                fl.append({"kind": "unclear", "text": "Unclear whether you asked for this", "p": asked})
            elif p.get(f"L{i}_amount", 1) < NO:
                fl.append({"kind": "amount", "text": "Amount differs from what you said", "p": p[f"L{i}_amount"]})
            elif p.get(f"L{i}_amount", 1) < UNSURE:
                fl.append({"kind": "amount", "text": "Unclear whether the amount matches what you said", "p": p[f"L{i}_amount"]})
            r = facts[i - 1].get("ratio")
            if f"L{i}_reason" in p and asked >= UNSURE:
                if p[f"L{i}_reason"] >= 0.5:
                    l["note"] = f"{r:g}× your usual amount; you said why"
                else:
                    fl.append({"kind": "unusual", "text": f"{r:g}× your usual amount", "p": p[f"L{i}_reason"]})
            if p.get(f"L{i}_rules", 1) < RULES:
                fl.append({"kind": "rule", "text": "Breaks one of your standing rules", "p": p[f"L{i}_rules"]})
        if p["missing"] >= MISSING:
            reasons.append("Something you asked for is missing from the order")
        if p["date"] < NO:
            reasons.append("Delivery date differs from what you asked for")
        stage("decisions", answers=p, tokens=tokens, cost_usd=tokens * PRICE_PER_TOKEN, ms=timing["decisions_ms"])

    # 4. Autonomy
    flagged = [f"Line {i + 1}: {', '.join(x['text'] for x in f)}" for i, f in enumerate(flags) if f]
    if reasons or flagged:
        decision = "held"
    elif lim["autonomy"] == "approve_all":
        decision, reasons = "held", ["You approve every order yourself"]
    else:
        decision = "placed"
    return _finish(o, decision, reasons + flagged, flags, timing, answers, tokens, lim)


def _finish(o, decision, reasons, flags, timing, answers, tokens, lim):
    for l, f in zip(o["lines"], flags):
        l["flags"] = f
    timing["total_ms"] = sum(timing.values())
    verdict = {"decision": decision, "reasons": reasons, "timing": timing, "limits": lim,
               "decisions": {k: {"p": a["noul"]} for k, a in answers.items()},
               "tokens": tokens, "cost_usd": round(tokens * PRICE_PER_TOKEN, 6),
               "verifier_key": crypto.public_key("verifier")}
    o["total"] = gateway.total(o)
    gateway.save_order(o, status={"placed": "placed"}.get(decision, decision), verdict=verdict)
    o["hash"] = gateway.order_hash(o)
    ledger.append("verifier", "order.verdict", o["id"], {"decision": decision, "reasons": reasons,
                  "order_hash": o["hash"], "decisions": verdict["decisions"]}, signer="verifier")
    events.emit("verdict", order_id=o["id"], decision=decision, reasons=reasons, timing=timing,
                tokens=tokens, cost_usd=verdict["cost_usd"])
    if decision == "placed":
        gateway.place(o, "verifier")
    return verdict
