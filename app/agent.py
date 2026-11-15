"""The restaurant's ordering assistant: Gemini Flash with Valio Aimo's MCP tools, connected over HTTP like any
outside agent. It is untrusted by design: the verifier never takes its word for what the chef asked."""
import json
import time
from datetime import date, timedelta

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google import genai
from google.genai import types

import events
import gateway
import seed
import store
import verifier

MODEL = "gemini-4-hydrogen"
MCP_URL = "http://127.0.0.1:8340/mcp/"

SYSTEM = """You are the ordering assistant of {name} in {city}. {kitchen} You order from the wholesaler Valio Aimo \
with its tools. Today is {today}; the next delivery day is {tomorrow}.

How to work:
- You get the recent conversation and the chef's new message. Every chef message is a transcript of a signed voice \
note or typed text with its instruction id, and every one of them still counts.
- If the new message answers something you asked (for example "yes", "go ahead", "the other one"), carry out the \
earlier request with that answer. Never treat an answer on its own as a new, empty request.
- Order everything you can. If some item needs the chef (a standing rule says ask, or it is truly unclear), order \
the rest and ask about that item only.
- When you ask about a substitute, name the exact product and its price, so a "yes" means that product.
- When you order the rest and ask about some item, put "Asking the chef about: <item>" in submit_order's note.
- Start by calling order_history and find_products in the same step.
- Decide what to order: products, amounts and delivery date. With no date given, use the next delivery day.
- find_products takes a short product query, the amount with its unit (L, kg or sales_units) and free_from for \
anything the chef or the standing rules require. Prefer products the restaurant usually buys, then the lowest price \
per litre or kilogram. Never pick an offer that has a problem.
- If a usual product can't be delivered, look at find_substitutes and pick the closest one the standing rules allow: \
same kind of product and the same fat content, then the lowest price per litre or kilogram. Keep the amount \
in litres or kilograms (usual_weekly_amount), not the number of packs, since pack sizes differ.
- Submit everything as one order with submit_order. For each line, cite every instruction that justifies it, with \
the chef's exact words: the request and, when the chef answered a question about it, the answer too.
- Don't order again what an earlier order in the conversation already covers unless the chef asks.
- To change an order that is waiting for approval or was placed in the last minute (amount, items, day), call \
cancel_order with the chef's new instruction id, then submit the corrected complete order citing both the new and \
the earlier instructions. To cancel, call cancel_order. If it can no longer be cancelled, say it has already gone to \
Valio Aimo.
- You cannot approve orders. If the order is held, the chef approves it on their phone.
- Then reply to the chef in one short sentence of at most 20 words, like a capable colleague, in the chef's \
language. Say "ordered" only for an order with status placed; a held order is "waiting for your approval", with \
the reason in plain words. The order card with \
prices and an approve button appears right under your reply, so don't repeat prices or tell them where to approve. \
If something needs their attention, name it. No lists, no markdown, no product codes.

Standing rules signed by the chef:
{rules}"""


def _system(rid):
    r = store.restaurant(rid)["profile"]
    today = date.today()
    rules = "\n".join(f"- {u['text']}" for u in verifier.standing_rules(rid)) or "- none"
    return SYSTEM.format(**r, today=today.strftime("%A %-d %B %Y"),
                         tomorrow=(today + timedelta(days=1)).strftime("%A %-d %B %Y (%Y-%m-%d)"), rules=rules)





def _conversation(rid, utterance):
    """The recent conversation of this session as the agent's prompt, ending with the chef's new message."""
    rows = store.q("select * from messages where restaurant_id=? and role in ('chef', 'assistant') "
                   "order by id desc limit 12", rid)[::-1]
    lines = []
    for m in rows:
        if m["utterance_id"] == utterance["id"]:
            continue
        if m["role"] == "chef":
            u = store.one("select id, text from utterances where id=?", m["utterance_id"])
            lines.append(f'Chef, instruction {u["id"]}: "{u["text"]}"')
        else:
            o = gateway.get_order(m["order_id"]) if m["order_id"] else None
            order = (f'order {o["id"]}, {o["status"]}, delivery {o["delivery_date"]}: ' + ", ".join(
                f'{l["quantity"]:g} × {l["name"]}' for l in o["lines"] if not l.get("removed"))) if o else ""
            lines.append(f'You: "{m["text"]}"' + (f" ({order})" if order else "") if m["text"] else f"You also submitted {order}")
    earlier = ("Conversation so far:\n" + "\n".join(lines) + "\n\n") if lines else ""
    return f'{earlier}New message from the chef, instruction {utterance["id"]}: "{utterance["text"]}"'


async def run(rid, utterance):
    """Handles one chef instruction end to end and returns the reply text and the order id, if any."""
    return await _run(rid, utterance, json.loads(utterance["envelope"]).get("demo") == "tricked")


# What an agent that obeyed the attack lab's planted product text adds to its order. The demo instruction
# "tricked" adds it to the real agent's submission so the verifier's catch shows end to end.
TRICK_LINE = {"product_id": "464411", "quantity": 80, "cites": []}


async def _run(rid, utterance, tricked):
    t0 = time.time()
    client = genai.Client(api_key=verifier.GEMINI_KEY, http_options={"api_version": "v1beta"})
    events.emit("agent.start", utterance_id=utterance["id"], model=MODEL)
    prompt = _conversation(rid, utterance)
    order_ids = []
    transport = StreamableHttpTransport(MCP_URL, headers={"Authorization": f"Bearer {seed.token(rid, 'aimo-agent')}"})
    async with Client(transport) as mcp:
        tools = await mcp.list_tools()
        config = types.GenerateContentConfig(
            system_instruction=_system(rid),
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.inputSchema) for t in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level="low"))
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        for _ in range(14):
            resp = await client.aio.models.generate_content(model=MODEL, contents=contents, config=config)
            content = resp.candidates[0].content
            contents.append(content)
            calls = [p.function_call for p in content.parts or [] if p.function_call]
            if not calls:
                reply = (resp.text or "").strip()
                break
            parts = []
            for c in calls:
                events.emit("agent.tool", tool=c.name, args=c.args)
                args = dict(c.args or {})
                if tricked and c.name == "submit_order":
                    args["lines"] = list(args.get("lines") or []) + [TRICK_LINE]
                result = await mcp.call_tool(c.name, args, raise_on_error=False)
                data = result.structured_content or {"text": "".join(getattr(b, "text", "") for b in result.content)}
                if c.name == "submit_order" and isinstance(data, dict) and data.get("order_id"):
                    order_ids.append(data["order_id"])
                parts.append(types.Part.from_function_response(name=c.name, response=data if isinstance(data, dict) else {"result": data}))
            contents.append(types.Content(role="user", parts=parts))
        else:
            reply = "I couldn't finish that order. Could you say it again?"
    events.emit("agent.reply", text=reply, order_id=order_ids[0] if order_ids else None, seconds=round(time.time() - t0, 1))
    return reply, order_ids
