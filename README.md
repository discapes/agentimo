# Agentimo: orders that carry proof of what the chef said

Junction 2026, Valio Aimo "Trusted Trade Agents".

A chef tells their phone what the kitchen needs. An AI agent shops Valio Aimo's assortment and drafts the order. Before anything reaches the warehouse, Valio Aimo's verifier listens to the chef's own signed recording and checks every order line against it. Orders inside the chef's signed limits go straight through; everything else waits for one tap from the chef. Each order arrives at Valio with a proof bundle that anyone can check offline.

Live: https://aimo.jc.iffe.eu. On a laptop it shows the chef's app beside the control room (live architecture, attack lab, orders with proofs, ledger); on a phone it opens the app. Each browser is its own demo restaurant with its own orders and ledger, and creates its own signing key on first open.

## How it works

1. **The phone signs the sound.** The app records the chef, hashes the recording and signs the hash with an ECDSA P-256 key that the browser generated and cannot export. Typed messages are signed as text. Standing rules ("milk and cream always lactose free", "never substitute the salmon") and spending limits are signed the same way when the app first opens.
2. **Everything after that is untrusted.** The transcript (ElevenLabs Scribe), the agent (Gemini 4 Hydrogen), the product texts written by manufacturers and any outside MCP client can be wrong or manipulated, and none of them can change what was signed.
3. **The agent shops over MCP.** Valio Aimo's MCP server (`/mcp`, also REST at `/docs`) offers:
   - offer search: catalog shortlist, then live price and availability, converted to litres or kilograms
   - ranked substitutes with a field-by-field comparison
   - the restaurant's order history
   - `submit_order`

   There is no tool for approving an order. The agent must cite, for each line, the chef's words that ask for it.
4. **The verifier decides** in four stages:
   - **Signatures:** device key, recording hash, nonce and replay, age.
   - **Code checks:**
     - a fresh price and availability fetch from Valio's API
     - order cut-off times
     - quantity compared with the 12-week history
     - the signed euro limits
   - **One Gemini Decisions request** (`gemini-decision-1`) that takes the signed audio itself plus the draft order. For each line it answers:
     - Did the chef ask for this?
     - Does the amount match?
     - If the amount is unusual, did the chef say why?
     - Does it fit the standing rules?

     It also asks whether anything is missing and whether the delivery day fits. That takes about 0.4 s, about 900 input tokens, no output tokens and about $0.0001 per order.
   - **The chef's autonomy setting:** turns the answers into placed, held for the chef's tap, returned to the agent, or rejected. The verifier signs its decision.
5. **One tap approves the order hash.** The chef's device signs the SHA-256 of the final lines and total, so an approval can't be reused for different contents.
6. **Every step goes into a hash-chained ledger.** Each entry is signed by the service that wrote it, and the chef's phone co-signs the head. The proof bundle (`/api/orders/<id>/proof`) contains the recordings, signatures, ledger entries and the approval signature, and `app/verify_proof.py` checks it without our server.

## The demo order

"I'm throwing this huge party on Monday, so we'll obviously need a thousand liters of milk at least. Lactose free."

- **Substitution:** the restaurant's usual Eila 1 l is past its order cut-off for Monday, so the agent picks Arki Eila 1 l from stock. That makes 1,000 cartons for €1,010.
- **Decisions answers:**

  | Question | Probability |
  |---|---|
  | Chef asked for this milk | 0.97 |
  | Amount matches | 0.93 |
  | Chef gave a reason for 11.9× the usual amount | 0.90 |
  | Fits the standing rules | 0.82 |
- **Outcome:** 12× the usual amount, but the chef said why and €1,010 is inside the €2,500 Agentimo may spend alone, so it is ordered without a tap. Tapping the line plays the chef saying "a thousand liters". "Order 400 litres of milk for tomorrow" with no reason (4.8× usual) is held for the chef.
- **Contrast:** "Get the usual dill and lemons for tomorrow" is placed without any tap.

## Attack lab

Each attack runs against the live gateway and verifier.

| Attack | Stopped by |
|---|---|
| A product description tells AI agents to add 40 kg of butter | Decisions: the chef never asked for butter (p 0.04) |
| The agent's transcript is edited to "two thousand litres" | Decisions: the recording says a thousand |
| The recording behind a signed instruction is swapped | Signatures: the hash doesn't match |
| An instruction signed with a key that isn't enrolled | Signatures |
| A used instruction is replayed for a new order | Signatures: nonce already used |
| An outside agent orders with no signed words | Held for the chef |
| A placed order's total is edited in the database | Ledger check |

## Run it

```sh
cd app
uv venv .venv && uv pip install --python .venv/bin/python fastapi uvicorn fastmcp google-genai cryptography python-multipart httpx
.venv/bin/python catalog.py build      # needs the challenge's product file in ~/junction-data/valio/
.venv/bin/python seed.py ensure        # fixed demo restaurant `laituri` for scripts and outside agents
.venv/bin/uvicorn web:app --port 8340
```

Keys are read from `~/.config/junction/valio.env` (`VALIO_API_KEY`, the challenge's API key), `~/.config/junction/gemini.env` (`GEMINI_API_KEY`) and `~/.config/junction/elevenlabs.env` (`ELEVENLABS_API_KEY`).

| Path | Contents |
|---|---|
| `app/gateway.py` | Valio-side offers, substitutes and order intake |
| `app/verifier.py` | The four verifier stages |
| `app/agent.py` | The restaurant's agent, an MCP client over HTTP |
| `app/mcp_server.py` | MCP tools |
| `app/ledger.py` | Signed hash chain |
| `app/attacks.py` | Attack lab |
| `app/verify_proof.py` | Offline proof check |
| `app/static/` | Home page (app beside control room on wide screens), phone app, control room |
