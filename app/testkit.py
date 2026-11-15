"""Signs utterances with the server-held test device, the way the phone does, for tests and offline runs."""
import json
import secrets
from datetime import datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import crypto
import instructions
import seed


def sign_envelope(key, env):
    envelope = json.dumps(env)
    r, s = decode_dss_signature(key.sign(envelope.encode(), ec.ECDSA(hashes.SHA256())))
    return envelope, crypto.b64(r.to_bytes(32) + s.to_bytes(32))


def utterance(kind, audio=None, mime="audio/ogg", text=None, key=None, rid=seed.RID, **extra):
    seed.ensure(rid)
    test_key, did = seed.test_device(rid)
    env = {"v": 1, "restaurant": rid, "device": did, "kind": kind, "nonce": secrets.token_hex(12),
           "ts": datetime.now().astimezone().isoformat(timespec="seconds"), **extra}
    env |= {"audio_sha256": crypto.sha256(audio), "mime": mime} if audio else {"text": text}
    envelope, sig = sign_envelope(key or test_key, env)
    return instructions.receive(envelope, sig, audio)


def rules(rid=seed.RID):
    return utterance("rule", text=seed.RULES, limits=seed.LIMITS, replaces=True, rid=rid)
