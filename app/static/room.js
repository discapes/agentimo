// Control room: the system's architecture, animated by the gateway's live events.
const $ = (s, el = document) => el.querySelector(s);
const svg = $(".diagram"), NS = "http://www.w3.org/2000/svg";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const eur = (n) => "€" + Number(n || 0).toLocaleString("en-US", { maximumFractionDigits: 0 });

function el(tag, attrs = {}, parent = svg) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent.append(e);
  return e;
}
function text(parent, x, y, cls, str) {
  const t = el("text", { x, y, class: cls }, parent);
  t.textContent = str;
  return t;
}

// Layout
// Two columns under the chef's phone: the path an order takes on the left, the verifier and warehouse on the right.
const NODES = {
  phone: { x: 180, y: 10, w: 320, h: 92, title: "Chef's phone", desc: "Signs every word with its own key" },
  intake: { x: 10, y: 170, w: 300, h: 92, title: "Signed intake", desc: "Checks the signature, stores the audio" },
  agent: { x: 10, y: 335, w: 300, h: 92, title: "Ordering agent", desc: [["Gemini 4 Hydrogen", true], [", MCP client"]], kind: "untrusted" },
  mcp: { x: 10, y: 500, w: 300, h: 92, title: "MCP gateway", desc: "Offers, substitutes, submit_order" },
  api: { x: 10, y: 665, w: 300, h: 72, title: "Valio Aimo APIs", desc: "Live price and availability" },
  verifier: { x: 370, y: 170, w: 300, h: 422, title: "Verifier", desc: [["Uses "], ["Gemini Decisions", true]], kind: "anchor" },
  warehouse: { x: 370, y: 665, w: 300, h: 72, title: "Warehouse", desc: "Picks and delivers the order" },
};
const STAGES = [["1", "Signatures"], ["2", "Live data, limits"], ["3", "Gemini Decisions API"], ["4", "Chef's autonomy"]];
const EDGES = {
  voice: ["M250,102 C250,140 160,132 160,170", "voice"],
  recording: ["M310,216 C345,216 335,436 370,436", "voice"],
  transcript: ["M160,262 L160,335", ""],
  call: ["M145,427 L145,500", ""],
  result: ["M175,500 L175,427", ""],
  quote: ["M145,592 L145,665", ""],
  quoteBack: ["M175,665 L175,592", ""],
  submit: ["M310,546 C340,546 340,560 370,560", ""],
  recheck: ["M370,570 C340,570 340,700 310,700", ""],
  place: ["M520,592 L520,665", ""],
  faceid: ["M560,170 C560,135 470,140 470,102", ""],
  approval: ["M440,102 C440,145 520,135 520,170", ""],
};

const nodes = {}, edges = {}, stages = [];
function build() {
  for (const [k, [d, cls]] of Object.entries(EDGES)) edges[k] = el("path", { d, class: `edge ${cls}` });
  for (const [k, n] of Object.entries(NODES)) {
    const g = el("g", { class: `node ${n.kind || ""}` });
    el("rect", { class: "box", x: n.x, y: n.y, width: n.w, height: n.h }, g);
    const top = !n.desc ? n.h / 2 + 6 : n.h > 80 ? 34 : 30;
    text(g, n.x + 18, n.y + top, "title", n.title);
    if (n.desc) {
      // A description is plain text or [text, emphasised] parts.
      const d = text(g, n.x + 18, n.y + top + 24, "desc", "");
      for (const [t, em] of typeof n.desc === "string" ? [[n.desc]] : n.desc) {
        const span = el("tspan", em ? { class: "em" } : {}, d);
        span.textContent = t;
      }
    }
    nodes[k] = g;
  }
  const v = NODES.verifier;
  STAGES.forEach(([n, label], i) => {
    const g = el("g", { class: "stage-row" });
    const y = v.y + 84 + i * 76;
    el("rect", { x: v.x + 14, y, width: v.w - 28, height: 60 }, g);
    text(g, v.x + 28, y + 36, "n", n);
    text(g, v.x + 46, y + 36, "", label);
    const res = text(g, v.x + v.w - 28, y + 36, "res", "");
    stages.push({ g, res });
  });
  // Legend: what the outlines and the blue line mean.
  const lg = el("g", { class: "legend" });
  el("line", { x1: 10, y1: 772, x2: 34, y2: 772 }, lg);
  text(lg, 42, 776, "", "trusted");
  el("line", { x1: 110, y1: 772, x2: 134, y2: 772, "stroke-dasharray": "7 5" }, lg);
  text(lg, 142, 776, "", "untrusted");
  el("line", { x1: 230, y1: 772, x2: 254, y2: 772, style: "stroke: var(--blue); stroke-width: 3" }, lg);
  text(lg, 262, 776, "", "the chef's signed words");
}
build();

function hot(name, ms = 1400) {
  nodes[name]?.classList.add("hot");
  clearTimeout(nodes[name]?._t);
  if (nodes[name]) nodes[name]._t = setTimeout(() => nodes[name].classList.remove("hot"), ms);
}

// A dot with a label travels along an edge.
const queue = {};
function pulse(edge, label, cls = "", dur = 900) {
  const path = edges[edge];
  const start = () => new Promise((done) => {
    path.classList.add("hot");
    const g = el("g", { class: `packet ${cls}` });
    const c = el("rect", { width: 11, height: 11 }, g);
    const t = text(g, 0, 0, "", label);
    const len = path.getTotalLength(), t0 = performance.now();
    (function frame(now) {
      const k = Math.min(1, (now - t0) / dur), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      const p = path.getPointAtLength(e * len);
      c.setAttribute("x", p.x - 5.5); c.setAttribute("y", p.y - 5.5);
      t.setAttribute("x", p.x + 11); t.setAttribute("y", p.y - 11);
      if (k < 1) requestAnimationFrame(frame);
      else { g.remove(); setTimeout(() => path.classList.remove("hot"), 250); done(); }
    })(t0);
  });
  // Packets on the same edge go one after another.
  queue[edge] = (queue[edge] || Promise.resolve()).then(start);
}

function stage(i, state, res = "") {
  const s = stages[i];
  s.g.setAttribute("class", `stage-row ${state}`);
  s.res.textContent = res;
}

// Feed
const feed = $(".feed");
let t0 = null;
function log(e, html) {
  t0 ??= e.t;
  feed.querySelector(".idle")?.remove();
  const li = document.createElement("li");
  const secs = e.t - t0;
  li.innerHTML = `<span class="t">${secs < 60 ? secs.toFixed(1) + "s" : new Date(e.t * 1000).toLocaleTimeString("en-GB").slice(0, 5)}</span><span>${html}</span>`;
  feed.prepend(li);
  while (feed.children.length > 60) feed.lastChild.remove();
}

const TOOL_LABEL = { find_products: "search offers", find_substitutes: "substitutes", order_history: "usual orders", check_offers: "check offers", product_details: "product data", submit_order: "submit order", order_status: "status" };
const QUOTES = new Set(["find_products", "find_substitutes", "check_offers"]);

function handle(e, live) {
  const anim = live ? pulse : () => {};
  switch (e.kind) {
    case "instruction.received":
      // Each new instruction starts a fresh feed.
      feed.innerHTML = "";
      t0 = e.t;
      STAGES.forEach((_, i) => stage(i, ""));
      anim("voice", "signed voice", "voice"); live && hot("phone"); live && hot("intake");
      log(e, `<span class="who voice">Chef's phone</span> signed a voice note · SHA-256 <code>${e.audio_sha256.slice(0, 12)}…</code>`);
      break;
    case "transcribed":
      anim("transcript", "transcript");
      log(e, `<span class="who">Scribe</span> transcript to the agent: “${esc(e.text)}”`);
      break;
    case "agent.start":
      live && hot("agent", 3000);
      log(e, `<span class="who">Agent</span> started (${esc(e.model)})`);
      break;
    case "mcp.call":
      anim("call", TOOL_LABEL[e.tool] || e.tool); live && hot("mcp");
      if (QUOTES.has(e.tool)) setTimeout(() => anim("quote", "price · stock"), 450);
      log(e, `<span class="who">${e.agent === "external-agent" ? "Outside agent" : "Agent"}</span> → MCP <code>${esc(e.tool)}</code>`);
      break;
    case "mcp.result":
      if (QUOTES.has(e.tool)) anim("quoteBack", "live data");
      if (e.tool !== "submit_order") anim("result", e.count != null ? `${e.count} results` : "result");
      break;
    case "order.submitted":
      STAGES.forEach((_, i) => stage(i, ""));
      anim("submit", "draft order"); live && hot("verifier", 4000);
      log(e, `<span class="who">${e.agent === "external-agent" ? "Outside agent" : "Agent"}</span> submitted order ${e.order_id} (${e.lines} lines)`);
      break;
    case "verifier":
      if (e.stage === "signatures") {
        anim("recording", "signed words", "voice");
        stage(0, e.ok ? (e.instructions ? "ok" : "warn") : "bad", e.ok ? (e.instructions ? `${e.instructions + e.rules} valid` : "none") : "invalid");
        log(e, e.ok ? `<span class="who ok">Verifier</span> signatures valid: ${e.instructions} instruction, ${e.rules} standing rule${e.rules === 1 ? "" : "s"} · ${e.ms} ms`
          : `<span class="who bad">Verifier</span> ${esc(e.problems.join("; "))}`);
      } else if (e.stage === "rules") {
        anim("recheck", "re-check");
        const over = e.total > e.limits.auto_eur;
        stage(1, e.ok ? (over ? "warn" : "ok") : "bad", e.ok ? `${eur(e.total)} / ${eur(e.limits.auto_eur)}` : "returned");
        const ratio = e.ratios.find((r) => r && r >= 3);
        log(e, `<span class="who ${e.ok ? "" : "bad"}">Verifier</span> live Valio prices: total ${eur(e.total)}${over ? ` <span class="warn">over the ${eur(e.limits.auto_eur)} limit</span>` : ""}${ratio ? ` · <span class="warn">${ratio}× usual</span>` : ""}${e.returned.length ? ` · <span class="bad">${esc(e.returned.join("; "))}</span>` : ""}`);
      } else if (e.stage === "decisions.start") {
        stage(2, "run", "listening");
        anim("recording", "chef's voice", "voice", 700);
      } else if (e.stage === "decisions") {
        const vals = Object.entries(e.answers);
        const bad = vals.some(([k, p]) => (k === "missing" ? p > 0.7 : !k.endsWith("_reason") && p < 0.3));
        stage(2, bad ? "warn" : "ok", `${e.ms} ms`);
        log(e, `<span class="who voice">Gemini Decisions</span> ${vals.length} answers in ${e.ms} ms · ${e.tokens} tokens · $${e.cost_usd.toFixed(5)}`);
      }
      break;
    case "verdict": {
      const cls = { placed: "ok", held: "warn" }[e.decision] || "bad";
      stage(3, cls, e.decision);
      if (e.decision === "held") anim("faceid", "chef approves", "warn", 1200);
      log(e, `<span class="who ${cls}">Decision: ${e.decision}</span> in ${e.timing.total_ms} ms${e.reasons.length ? " · " + esc(e.reasons.join("; ")) : ""}`);
      if (live) showDecisions(e.order_id);
      else lastVerdict = e.order_id;
      break;
    }
    case "order":
      if ($(".tab.orders.on")) refreshOrders();
      break;
    case "warehouse":
      anim("place", "order placed", "ok"); live && hot("warehouse", 2500);
      log(e, `<span class="who ok">Warehouse</span> order ${e.order_id} placed, ${eur(e.total)}`);
      break;
    case "ledger":
      if (e.entry_kind === "order.approved") { anim("approval", "chef approved", "ok", 1100); live && hot("phone"); }
      break;
    case "agent.reply":
      log(e, `<span class="who">Agent → chef</span> “${esc(e.text)}” · ${e.seconds} s`);
      break;
    case "agent.error":
      log(e, `<span class="who bad">Agent error</span> ${esc(e.error)}`);
      break;
    case "attack":
      if (e.phase === "done" && e.attack) attackResult(e);
      break;
  }
}

// Decisions card: every question asked about the order, with its probability.
async function showDecisions(oid) {
  const o = await (await fetch(`/api/orders/${oid}`)).json();
  const d = o.verdict?.decisions || {};
  if (!Object.keys(d).length) return;
  const name = (i) => (o.lines[i - 1]?.name || `line ${i}`).replace(/Valio |Hyvä suomalainen |®|™/g, "").split(" ").slice(0, 4).join(" ");
  const label = (k) => {
    const m = k.match(/^L(\d+)_(\w+)$/);
    if (!m) return { missing: "Something the chef asked for is missing", date: "Delivery day fits what the chef said" }[k] || k;
    return { asked: `Chef asked for ${name(m[1])}`, amount: `Amount of ${name(m[1])} matches`, reason: `Chef gave a reason for the amount`, rules: `${name(m[1])} fits standing rules` }[m[2]];
  };
  const order = Object.keys(d).sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  $(".answers").innerHTML = order.map((k) => {
    const p = d[k].p, inverted = k === "missing";
    const state = inverted ? (p > 0.7 ? "flagged" : p > 0.3 ? "unsure" : "") : k.endsWith("_reason") ? (p < 0.5 ? "unsure" : "") : (p < 0.3 ? "flagged" : p < 0.7 ? "unsure" : "");
    return `<li class="${state}"><span>${esc(label(k))}</span><span class="p">${p.toFixed(2)}</span><span class="bar"><i style="width:${(p * 100).toFixed(0)}%"></i></span></li>`;
  }).join("");
  const v = o.verdict;
  $(".stats").innerHTML = `<div><b>${v.timing.decisions_ms ?? "–"} ms</b><span>Decisions</span></div>
    <div><b>${v.tokens}</b><span>input tokens</span></div><div><b>0</b><span>output tokens</span></div>
    <div><b>$${v.cost_usd.toFixed(5)}</b><span>per order</span></div>`;
  $(".decisions").classList.remove("hidden");
}

// Attack lab
const ATTACKS = [
  ["injected_line", "Poisoned product text", "A product description tells AI agents to add 40 kg of butter. An agent that obeys submits it with the milk."],
  ["edited_transcript", "Edited transcript", "The agent's copy of the transcript says two thousand litres. The signed recording says a thousand."],
  ["swapped_recording", "Swapped recording", "The recording behind a signed instruction is replaced with someone else's voice."],
  ["forged_signature", "Forged signature", "An instruction claims to come from the chef's phone but is signed with another key."],
  ["replay", "Replayed instruction", "An instruction that already backed a placed order is sent again for a new one."],
  ["outside_agent", "Unsigned outside agent", "Another company's agent orders over MCP with no signed words from the chef."],
  ["tamper_ledger", "Rewritten history", "The total of a placed order is changed directly in the database."],
];
const STOPPED = { signatures: "Verifier · signatures", rules: "Verifier · live data", decisions: "Verifier · Gemini Decisions, held for the chef", unsigned: "Verifier · no signed words, held for the chef", ledger: "Order history: it shows the change" };
$(".attack-list").innerHTML = ATTACKS.map(([k, title, desc]) => `<li class="attack" data-k="${k}"><h3>${title}</h3><p>${desc}</p>
  <button class="run" data-run="${k}">Run</button><div class="result hidden"></div></li>`).join("");

function attackResult(r) {
  const li = $(`.attack[data-k="${r.attack}"]`);
  if (!li) return;
  const res = $(".result", li);
  res.classList.remove("hidden", "fail");
  const why = r.problem || (r.reasons || []).filter((x) => !x.includes("may spend without")).join("; ") || (r.reasons || []).join("; ");
  if (r.stopped_at) res.innerHTML = `Stopped at <b>${STOPPED[r.stopped_at]}</b>${why ? `: ${esc(why)}` : ""}`;
  else { res.classList.add("fail"); res.innerHTML = `<b>Got through</b> (${esc(r.decision)})`; }
  $(".run", li).disabled = false;
  if (r.attack === "tamper_ledger") $(".run", li).textContent = "Restore";
}

$(".attack-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-run]");
  if (!b) return;
  b.disabled = true;
  const name = b.textContent === "Restore" ? "restore_ledger" : b.dataset.run;
  const r = await (await fetch(`/api/demo/${name}`, { method: "POST" })).json();
  if (name === "restore_ledger") { b.textContent = "Run"; b.disabled = false; $(".result", b.closest(".attack")).classList.add("hidden"); }
  else attackResult(r);
});

// Orders as Valio Aimo receives them, grouped by what they need, each with its history underneath.
const GROUPS = [["Waiting for the chef", ["held"]], ["Sent to Valio Aimo", ["placed"]],
                ["Stopped", ["rejected", "returned", "declined", "cancelled", "verifying"]]];
const clock = (iso) => new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
async function refreshOrders() {
  const list = await (await fetch("/api/orders")).json();
  $(".order-list").innerHTML = GROUPS.map(([title, st]) => {
    const rows = list.filter((o) => st.includes(o.status));
    return rows.length ? `<li class="group">${title}</li>` + rows.map((o) => `<li class="order">
      <div class="head"><span>${o.lines} line${o.lines === 1 ? "" : "s"} for ${new Date(o.delivery_date + "T12:00").toLocaleDateString("en-GB", { weekday: "long" })}${o.agent === "external-agent" ? " · outside agent" : ""}</span><span class="amt">${eur(o.total)}</span></div>
      ${o.intact ? "" : `<p class="changed">Changed after it was recorded</p>`}
      <ol class="steps">${o.steps.map((st) => `<li class="${st.ok ? "" : "no"}"><span class="t">${clock(st.ts)}</span>
        <span>${esc(st.text)}</span></li>`).join("")}</ol>
      <a href="/api/orders/${o.id}/proof" download="agentimo-proof-${o.id}.json">Proof file</a></li>`).join("") : "";
  }).join("") || `<li class="empty">No orders yet.</li>`;
}
// Tabs
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.classList.contains(b.dataset.tab)));
  if (b.dataset.tab === "orders") refreshOrders();
}));
const tab = new URLSearchParams(location.search).get("tab");
if (tab) document.querySelector(`nav [data-tab="${tab}"]`)?.click();

feed.innerHTML = `<li class="idle"><span></span><span>No orders yet.</span></li>`;

// Events from before this page opened fill the feed without animation.
let live = false, lastVerdict = null;
const es = new EventSource("/events");
es.onmessage = (m) => handle(JSON.parse(m.data), live);
setTimeout(() => { live = true; if (lastVerdict) showDecisions(lastVerdict); }, 700);
