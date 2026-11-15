import { device, key, newKey, send, signText } from "/static/sign.js";

const $ = (s, el = document) => el.querySelector(s);
if (new URLSearchParams(location.search).has("frame")) document.body.classList.add("framed");
const thread = $(".thread"), main = $("main");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const eur = (n) => "€" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
const UNITS = { ST: "pcs", KI: "cases", KG: "kg", PAK: "packs", RAS: "boxes", BAG: "bags", PRK: "tubs", BOT: "bottles", SK: "sacks", CS: "cases", KLE: "kegs", KRT: "cartons", KAN: "cans", PAL: "pallets" };
const day = (d) => new Date(d + "T12:00").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
const icon = {
  lock: '<svg viewBox="0 0 24 24"><path d="M7 11V8a5 5 0 0 1 10 0v3"/><rect x="4.5" y="11" width="15" height="10" rx="2.5"/></svg>',
  quote: '<svg viewBox="0 0 24 24"><path d="M5 11h4v6H5zM5 11c0-3 1-5 4-6M14 11h4v6h-4zM14 11c0-3 1-5 4-6"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  cross: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 8v5M12 16.5v.5"/><circle cx="12" cy="12" r="9"/></svg>',
  chart: '<svg viewBox="0 0 24 24"><path d="M4 19h16M7 16v-4M12 16V8M17 16V5"/></svg>',
  swap: '<svg viewBox="0 0 24 24"><path d="M7 7h11l-3-3M17 17H6l3 3"/></svg>',
};

function toast(text) {
  const t = Object.assign(document.createElement("div"), { className: "toast", textContent: text });
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}

// Rendering

function chefHTML(u, pending) {
  if (!u) return "";
  return `<article class="chef" data-u="${u.id}"><p class="said ${pending ? "pending" : ""}">${esc(u.text || "") || "…"}</p></article>`;
}

// Where the verifier's checks ended for this order.
function summary(o) {
  const v = o.verdict;
  if (!o.instruction_ids.length) return "No words from you back this order";
  if (v.decision === "rejected") return "Stopped at the signature check";
  if (v.decision === "returned") return "Stopped at the live price and stock check";
  return `Checked by Gemini Decisions in ${(v.timing.total_ms / 1000).toFixed(1)} s`;
}

function checksHTML(o) {
  const v = o.verdict;
  if (!v) return "";
  const flags = o.lines.flatMap((l) => (l.removed ? [] : l.flags || []).map((f) => f.kind));
  const checks = [
    [v.decision !== "rejected", "Signed on your phone, unchanged"],
    [v.decision !== "returned" && !flags.includes("availability"), "Prices and stock fetched from Valio Aimo again"],
    o.instruction_ids.length ? [!flags.some((k) => ["not_asked", "amount", "unclear"].includes(k)), "Every line matches what you said"] : [false, "No words from you back this order"],
    [!flags.includes("rule"), "Fits your standing rules"],
  ];
  const all = checks.every(([ok]) => ok);
  return `<details class="checks"><summary>${all ? icon.check : icon.cross}${summary(o)}</summary>
    <ul>${checks.map(([ok, t]) => `<li class="${ok ? "ok" : "no"}">${ok ? icon.check : icon.cross}${t}</li>`).join("")}</ul></details>`;
}

// Cited words trimmed to about six words.
const short = (q) => {
  const w = q.replace(/[.,!?]+$/, "").split(/\s+/);
  return w.length > 6 ? w.slice(0, 6).join(" ").replace(/[.,!?;:]+$/, "") + "…" : w.join(" ");
};
const plain = (name) => name.replace(/®|™/g, "").replace(/\s+/g, " ");

function lineHTML(o, l, i) {
  const held = o.status === "held";
  const unit = UNITS[l.sales_unit] || l.sales_unit.toLowerCase();
  const measure = l.measure && l.unit_measure && !(l.unit_measure === "kg" && unit === "kg") ? ` · ${num(l.measure)} ${l.unit_measure}` : "";
  const evidence = [
    ...l.cites.map((c) => `<p class="ev said-line">${icon.quote}<span>You said “${esc(short(c.quote))}”</span></p>`),
    ...(l.flags || []).map((f) => `<p class="ev flag ${f.kind}">${icon.alert}<span>${esc(f.text)}</span></p>`),
    l.note ? `<p class="ev">${icon.chart}<span>${esc(l.note.replace("your usual amount; you said why", "your usual, and you said why"))}</span></p>` : "",
    l.instead_of ? `<p class="ev">${icon.swap}<span>Replaces your usual ${esc(plain(l.instead_of).replace(/^Valio /, ""))}</span></p>` : "",
  ].join("");
  return `<li class="line ${l.removed ? "removed" : ""}">
    <div class="row"><span class="name">${esc(plain(l.name))}</span><span class="price">${l.line_total == null ? "" : eur(l.line_total)}</span></div>
    <div class="row"><span class="qty">${num(l.quantity)} ${unit}${measure}</span>
      ${held && !l.removed && (l.flags || []).length ? `<button class="remove" data-remove="${i}">Remove</button>` : ""}</div>
    ${evidence ? `<div class="evidence">${evidence}</div>` : ""}</li>`;
}

const STATUS = { held: "Needs your approval", placed: "Ordered", rejected: "Refused", returned: "Sent back to Agentimo", declined: "Declined", cancelled: "Cancelled", verifying: "Checking" };

function orderHTML(o) {
  const reasons = o.status === "placed" ? [] : (o.verdict?.reasons || []).filter((r) => !r.startsWith("Line "));
  const why = reasons.map((r) => r.replace("without the chef", "without you").replace(/^Instruction I-\w+: (\w)/, (_, c) => c.toUpperCase()));
  const undo = o.undo_until && o.undo_until * 1000 > Date.now();
  if (undo) setTimeout(load, o.undo_until * 1000 - Date.now() + 200);
  const actions = o.status === "held"
    ? `<div class="actions"><button class="approve" data-approve="${o.id}">${icon.check}Approve ${eur(o.total)}</button><p class="hint">or just say “yes, go ahead”</p><button class="decline" data-decline="${o.id}">Decline</button></div>`
    : o.status === "placed" ? `<p class="done">${icon.check}<span>${o.approved ? "Approved by you" : "Within your limits"}</span>${undo ? `<button class="undo" data-undo="${o.id}">Undo</button>` : ""}</p>` : "";
  return `<section class="order ${o.status}" data-o="${o.id}">
    <div class="top"><span class="status ${o.status}">${STATUS[o.status] || o.status}</span><span class="when">${new Date(o.delivery_date + "T12:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}</span></div>
    ${why.length ? `<div class="why">${why.map((r) => `<p>${esc(r)}${r.includes("Agentimo may spend") ? ` <button class="change" data-rules>Change</button>` : ""}</p>`).join("")}</div>` : ""}
    <ul class="lines">${o.lines.map((l, i) => lineHTML(o, l, i)).join("")}</ul>
    ${o.lines.some((l) => l.line_total != null) ? `<div class="total"><span>Total</span><span>${eur(o.total)}</span></div>` : ""}
    ${checksHTML(o)}${actions}</section>`;
}

let step = "Listening to you";
const STEPS = {
  "agent.start": "Thinking", order_history: "Looking at what you usually buy", find_products: "Searching Valio Aimo",
  find_substitutes: "Finding the closest substitute", check_offers: "Checking stock", product_details: "Reading product details",
  submit_order: "Checking the order against your voice",
};

// Replies that arrive while the app is open are spoken in the pool video's voice.
let lastSpoken = null;
const voice = new Audio();
function speak(msgs) {
  const id = msgs.filter((m) => m.role === "assistant" && m.text).at(-1)?.id ?? 0;
  if (lastSpoken !== null && id > lastSpoken) { voice.src = `/api/messages/${id}/voice`; voice.play().catch(() => {}); }
  lastSpoken = id;
}
// iOS only lets audio play later if an element was started from a tap.
const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";
function unlockAudio() { if (!voice.src) { voice.src = SILENCE; voice.play().catch(() => {}); } }

let seen = new Set();
async function load() {
  const msgs = await (await fetch("/api/chat")).json();
  speak(msgs);
  const ids = [...new Set(msgs.map((m) => m.order_id).filter(Boolean))];
  const orders = Object.fromEntries(await Promise.all(ids.map(async (id) => [id, await (await fetch(`/api/orders/${id}`)).json()])));
  // Each item carries a key; items already on screen skip their entrance animation on re-render.
  const keyed = (k, h) => h.replace(/^\s*<(\w+)/, `<$1 data-k="${k}"${seen.has(k) ? ' style="animation:none"' : ""}`);
  let html = "";
  msgs.forEach((m) => {
    if (m.role === "chef") html += keyed(`m${m.id}`, chefHTML(m.utterance));
    else {
      html += keyed(`m${m.id}`, m.role === "lab" ? `<article class="lab"><b>Attack lab</b><p>${esc(m.text)}</p></article>`
        : m.text ? `<article class="aimo"><div class="mark"></div><p>${esc(m.text)}</p></article>` : "");
      if (m.order_id && orders[m.order_id]) html += keyed(`o${m.order_id}`, orderHTML(orders[m.order_id]));
    }
  });
  const last = msgs.at(-1);
  if (last?.role === "lab" && !last.order_id) step = "Checking the order";
  if (last && (last.role === "chef" || (last.role === "lab" && !last.order_id))) html += keyed("working", workingHTML());
  thread.innerHTML = html;
  seen = new Set([...thread.querySelectorAll("[data-k]")].map((e) => e.dataset.k));
  reveal();
  cosign();
}

const workingHTML = () => `<div class="working"><div class="mark"></div><div class="dots"><i></i><i></i><i></i></div><span>${esc(step)}</span></div>`;

function setStep(text) {
  step = text;
  const w = $(".working span");
  if (w) w.textContent = text;
}

// Shows the newest exchange: the bottom of the chat if it fits, else the start of the exchange (the chef's message
// or the attack note) at the top, so a tall order card never pushes it out of view.
function reveal() {
  const starts = thread.querySelectorAll(".chef, .lab");
  const start = starts[starts.length - 1];
  if (!start) return void (main.scrollTop = 0);
  const fits = main.scrollHeight - start.offsetTop <= main.clientHeight;
  main.scrollTop = fits ? main.scrollHeight : start.offsetTop - 12;
}

// Approval: this device signs the order's hash, so the tap approves exactly the lines and total on the card.
async function approve(id) {
  const o = await (await fetch(`/api/orders/${id}`)).json();
  const r = await fetch(`/api/orders/${id}/approve`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ hash: o.hash, device: device().device, signature: await signText(o.hash) }) });
  if (!r.ok) toast((await r.json()).detail);
  load();
}

// The phone co-signs the ledger head it has seen, so history can't be rewritten without it.
let cosigned = null;
async function cosign() {
  const me = device();
  const head = await (await fetch("/api/ledger/head")).json();
  if (!me || !head.hash || head.hash === cosigned) return;
  cosigned = head.hash;
  fetch("/api/ledger/cosign", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ device: me.device, hash: head.hash, signature: await signText(head.hash) }) });
}

thread.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.remove != null) {
    await fetch(`/api/orders/${b.closest(".order").dataset.o}/lines/${b.dataset.remove}/remove`, { method: "POST" });
    load();
  } else if (b.dataset.approve) approve(b.dataset.approve).catch((err) => toast(err.message));
  else if (b.dataset.decline) { await fetch(`/api/orders/${b.dataset.decline}/decline`, { method: "POST" }); load(); }
  else if (b.dataset.undo) {
    const r = await fetch(`/api/orders/${b.dataset.undo}/undo`, { method: "POST" });
    if (!r.ok) toast((await r.json()).detail);
    load();
  } else if (b.dataset.rules != null) openRules();
});

// Recording

const BARS = 20;
const wave = $(".wave");
wave.innerHTML = "<i></i>".repeat(BARS);
const bars = [...wave.children];
let state = "idle", stream, recorder, stopRec;

async function record() {
  stream ??= await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext(), analyser = ctx.createAnalyser();
  ctx.createMediaStreamSource(stream).connect(analyser);
  const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m));
  recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  recorder.ondataavailable = (e) => chunks.push(e.data);
  const done = new Promise((r) => (recorder.onstop = r));
  recorder.start();
  document.body.classList.add("listening");
  const data = new Float32Array(2048), levels = new Array(BARS).fill(0), start = performance.now();
  let last = start, floor = 0, frames = 0, spoken = 0, quiet = 0, stopped = false;
  stopRec = () => { stopped = true; };
  await new Promise((resolve) => (function frame(now) {
    const dt = (now - last) / 1000; last = now;
    analyser.getFloatTimeDomainData(data);
    const level = Math.sqrt(data.reduce((a, v) => a + v * v, 0) / data.length);
    if (now - start < 300) floor = (floor * frames + level) / ++frames;
    const speaking = now - start > 300 && level > Math.max(floor * 2.5, 0.015);
    if (speaking) { spoken += dt; quiet = 0; } else if (spoken > 0) quiet += dt;
    levels.push(Math.min(1, level * 9)); levels.shift();
    bars.forEach((b, i) => { b.style.transform = `scaleY(${0.1 + 0.9 * levels[i]})`; });
    const secs = Math.floor((now - start) / 1000);
    $(".time").textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    if (stopped || (spoken > 0.6 && quiet > 1.3)) return resolve();
    requestAnimationFrame(frame);
  })(start));
  recorder.stop();
  await done;
  ctx.close();
  document.body.classList.remove("listening");
  return new Blob(chunks, { type: recorder.mimeType });
}

async function submit(payload) {
  document.body.classList.add("sending");
  step = "Listening to you";
  thread.insertAdjacentHTML("beforeend", chefHTML({ id: "pending", text: payload.text || "…", audio: false }, true));
  main.scrollTop = main.scrollHeight;
  try {
    await send("instruction", payload);
  } catch (err) {
    toast(err.message);
  }
  document.body.classList.remove("sending");
  load();
}

// Long-press the microphone to send the recording from the pool shoot instead of speaking.
let pressTimer, longPress = false;
const mic = $(".mic");
mic.addEventListener("pointerdown", () => {
  unlockAudio();
  longPress = false;
  if (state === "idle") pressTimer = setTimeout(async () => {
    longPress = true;
    const audio = await (await fetch("/static/pool-take.ogg")).blob();
    submit({ audio: new Blob([audio], { type: "audio/ogg" }), mime: "audio/ogg" });
  }, 700);
});
mic.addEventListener("pointerup", () => clearTimeout(pressTimer));
mic.addEventListener("click", async () => {
  if (longPress) return;
  if (state === "listening") return stopRec();
  if (state !== "idle") return;
  state = "listening";
  try {
    const audio = await record();
    state = "idle";
    if (audio.size > 2000) submit({ audio, mime: audio.type });
  } catch (err) {
    state = "idle";
    document.body.classList.remove("listening");
    toast(err.message);
  }
});

// Example orders on the empty screen.
const EXAMPLES = {
  usual: { text: "Get the usual dill and lemons for tomorrow." },
  salmon: { text: "We need 25 kilos of salmon for Monday." },
  tricked: { text: "Get the usual milk for Monday.", demo: "tricked" },
};
$(".suggest").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  unlockAudio();
  if (b.dataset.ex === "pool") {
    const audio = await (await fetch("/static/pool-take.ogg")).blob();
    return submit({ audio: new Blob([audio], { type: "audio/ogg" }), mime: "audio/ogg" });
  }
  submit(EXAMPLES[b.dataset.ex]);
});

// Start over: a new session for this browser. The top window reloads so the control room follows.
$(".restart").addEventListener("click", async () => {
  await fetch("/api/session/new", { method: "POST" });
  window.top.location.reload();
});

$(".kbd:not(.restart)").addEventListener("click", () => { $(".typed").classList.toggle("hidden"); $(".typed input").focus(); });
$(".typed").addEventListener("submit", (e) => {
  e.preventDefault();
  unlockAudio();
  const text = e.target.text.value.trim();
  if (!text) return;
  e.target.text.value = "";
  submit({ text });
});

// Live updates

const events = new EventSource("/events");
let ready = false;
events.onmessage = (m) => {
  const e = JSON.parse(m.data);
  if (!ready) return;
  if (e.kind === "chat" || e.kind === "order") load();
  else if (e.kind === "agent.start") setStep(STEPS["agent.start"]);
  else if (e.kind === "agent.tool") setStep(STEPS[e.tool] || "Working");
  else if (e.kind === "verdict") setStep(`Checked in ${e.timing.total_ms} ms`);
};
// Events replayed on connect are history; only live ones update the screen.
setTimeout(() => (ready = true), 800);

// First open: this browser makes its signing key and, if the restaurant has none yet, signs the default rules.
async function enroll() {
  const r = await fetch("/api/enroll", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jwk: await newKey() }) });
  localStorage.setItem("aimo-device", JSON.stringify(await r.json()));
}

// Standing rules and spending limits: shown on the start screen, edited in a sheet, signed on save.
let info;
const euros = (v) => Number(String(v).replace(/[^0-9.]/g, ""));
function showRules() {
  const r = info.rules.at(-1);
  if (!r) return;
  $(".rules-summary").innerHTML = `<b>Your rules</b> ${esc(r.text)} Agentimo orders alone up to ${eur(r.limits.auto_eur).replace(".00", "")}.
    <button class="change" data-rules>Change</button>`;
}
function openRules() {
  const r = info.rules.at(-1);
  const f = $(".sheet form");
  f.rules.value = r.text;
  f.auto.value = r.limits.auto_eur;
  f.max.value = r.limits.max_eur;
  $(".sheet").classList.remove("hidden");
}
$(".sheet form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, r = info.rules.at(-1);
  await send("rule", { text: f.rules.value.trim(), replaces: true,
    limits: { ...r.limits, auto_eur: euros(f.auto.value), max_eur: euros(f.max.value) } });
  info = await (await fetch("/api/me")).json();
  showRules();
  $(".sheet").classList.add("hidden");
  toast("Rules saved and signed");
  load();
});
$(".sheet .cancel").addEventListener("click", () => $(".sheet").classList.add("hidden"));
$(".empty").addEventListener("click", (e) => { if (e.target.closest("[data-rules]")) openRules(); });

(async () => {
  let me = device();
  info = await (await fetch(`/api/me?device=${me?.device || ""}`)).json();
  if (!me || !(await key()) || !info.enrolled) {
    await enroll();
    info = await (await fetch("/api/me")).json();
  }
  if (!info.rules.length) {
    await send("rule", { text: info.defaults.rules, limits: info.defaults.limits, replaces: true });
    info = await (await fetch("/api/me")).json();
  }
  showRules();
  load();
})();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
