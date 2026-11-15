// The phone's signing key: ECDSA P-256 generated in the browser as non-extractable, kept in IndexedDB.
// Everything the chef says or types is signed with it before it leaves the phone.

const DB = "aimo", STORE = "keys";

function idb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function idbDo(mode, fn) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode), req = fn(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
  });
}

export const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
export const unb64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - s.length % 4) % 4)), (c) => c.charCodeAt(0));
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
export const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
export const sha256 = async (data) => hex(await crypto.subtle.digest("SHA-256", data));

export async function newKey() {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  await idbDo("readwrite", (s) => s.put(pair, "device"));
  const { kty, crv, x, y } = await crypto.subtle.exportKey("jwk", pair.publicKey);
  return { kty, crv, x, y };
}

export const key = () => idbDo("readonly", (s) => s.get("device"));

export async function signText(text) {
  const k = await key();
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, k.privateKey, new TextEncoder().encode(text));
  return b64(sig);
}

const nonce = () => hex(crypto.getRandomValues(new Uint8Array(12)));

function isoNow() {
  const d = new Date(), off = -d.getTimezoneOffset(), p = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  const local = new Date(d.getTime() + off * 60000).toISOString().slice(0, 19);
  return `${local}${off >= 0 ? "+" : "-"}${p(off / 60)}:${p(off % 60)}`;
}

// Signs an utterance envelope and uploads it. A recording is signed by its SHA-256, typed text as itself.
export async function send(kind, { audio, mime, text, ...extra } = {}) {
  const me = JSON.parse(localStorage.getItem("aimo-device") || "null");
  const env = { v: 1, restaurant: me.restaurant, device: me.device, kind, nonce: nonce(), ts: isoNow(), ...extra };
  if (audio) Object.assign(env, { audio_sha256: await sha256(await audio.arrayBuffer()), mime });
  else env.text = text;
  const envelope = JSON.stringify(env);
  const form = new FormData();
  form.append("envelope", envelope);
  form.append("signature", await signText(envelope));
  if (audio) form.append("audio", audio, "voice");
  const r = await fetch("/api/utterances", { method: "POST", body: form });
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
}

export const device = () => JSON.parse(localStorage.getItem("aimo-device") || "null");
