"""Intake of what the chef signs on the phone: voice or typed instructions and standing rules."""
import json
import re
import secrets
import subprocess
from pathlib import Path

import httpx

import crypto
import events
import ledger
import store

AUDIO = store.DATA / "audio"
EXT = {"audio/webm": ".webm", "audio/mp4": ".m4a", "audio/ogg": ".opus", "audio/mpeg": ".mp3", "audio/wav": ".wav"}
ELEVEN_KEY = re.search(r"ELEVENLABS_API_KEY=(\S+)", (Path.home() / ".config/junction/elevenlabs.env").read_text())[1].strip("'\"")


class Refused(Exception):
    pass


def _scribe(path, language=None):
    r = httpx.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": ELEVEN_KEY}, timeout=60,
                   data={"model_id": "scribe_v2", "timestamps_granularity": "word",
                         **({"language_code": language} if language else {})},
                   files={"file": (path.name, path.read_bytes())})
    r.raise_for_status()
    return r.json()


def transcribe(path):
    """ElevenLabs Scribe v2 with word timings. The transcript is for people and the agent; the verifier never uses it.
    Chefs speak English or Finnish: Scribe detects which, and anything else is transcribed again as English."""
    res = _scribe(path)
    if res.get("language_code") not in ("eng", "fin"):
        res = _scribe(path, "en")
    words = [{"w": w["text"], "s": w["start"], "e": w["end"]} for w in res["words"] if w["type"] == "word"]
    return " ".join(w["w"] for w in words), words


def receive(envelope, signature, audio=None):
    """Stores a signed utterance after checking the signature against the enrolled device key."""
    env = json.loads(envelope)
    dev = store.one("select * from devices where id=? and kind='device'", env.get("device"))
    if not dev or dev["restaurant_id"] != env.get("restaurant"):
        raise Refused("unknown device")
    if not crypto.verify_device(json.loads(dev["public_jwk"]), envelope.encode(), signature):
        raise Refused("bad signature")
    if store.one("select 1 from utterances where json_extract(envelope, '$.nonce')=?", env["nonce"]):
        raise Refused("nonce already used")
    uid = ("I-" if env["kind"] == "instruction" else "R-") + secrets.token_hex(3).upper()
    row = {"id": uid, "restaurant_id": env["restaurant"], "kind": env["kind"], "envelope": envelope,
           "signature": signature, "device_id": dev["id"], "created": store.now(), "audio_path": None,
           "text": env.get("text"), "words": None}
    if audio is not None:
        if crypto.sha256(audio) != env.get("audio_sha256"):
            raise Refused("recording does not match the signed hash")
        AUDIO.mkdir(parents=True, exist_ok=True)
        path = AUDIO / (uid + EXT.get(env.get("mime", "").split(";")[0], ".webm"))
        path.write_bytes(audio)
        # The verifier hears a mono Opus copy made from the signed original.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-ac", "1", "-ar", "24000", "-c:a", "libopus",
                        "-b:a", "32k", path.with_suffix(".ogg")], check=True)
        row["audio_path"] = str(path)
        events.emit("instruction.received", utterance_id=uid, bytes=len(audio), audio_sha256=env["audio_sha256"])
        text, words = transcribe(path.with_suffix(".ogg"))
        row["text"], row["words"] = text, json.dumps(words)
        events.emit("transcribed", utterance_id=uid, text=text)
    if env["kind"] == "rule" and env.get("replaces"):
        store.run("update utterances set revoked=1 where restaurant_id=? and kind='rule'", env["restaurant"])
    store.insert("utterances", **row)
    ledger.append("chef", f"{env['kind']}.signed", uid, {"envelope": env, "device_signature": signature,
                  "device": dev["id"], **({"transcript": row["text"]} if audio is not None else {})})
    return row
