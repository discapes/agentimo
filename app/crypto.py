"""Signing and verification. Services sign with Ed25519; the chef's phone signs with a WebCrypto P-256 key."""
import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

KEYS = Path(__file__).with_name("data") / "keys"


def b64(b):
    return base64.b64encode(b).decode()


def unb64(s):
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


_keys = {}


def service_key(name):
    """The Ed25519 key of a service (gateway, verifier, agent), created on first use."""
    if name not in _keys:
        path = KEYS / f"{name}.pem"
        if not path.exists():
            KEYS.mkdir(parents=True, exist_ok=True)
            path.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        _keys[name] = serialization.load_pem_private_key(path.read_bytes(), None)
    return _keys[name]


def public_key(name):
    return b64(service_key(name).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def sign(name, data):
    return b64(service_key(name).sign(data))


def verify_service(name, data, sig):
    try:
        service_key(name).public_key().verify(unb64(sig), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_device(jwk, data, sig):
    """Checks a WebCrypto ECDSA P-256 / SHA-256 signature (raw r||s) over `data`."""
    try:
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(jwk["x"])), int.from_bytes(unb64(jwk["y"])),
                                            ec.SECP256R1()).public_key()
        raw = unb64(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def device_fingerprint(jwk):
    return sha256(canonical({k: jwk[k] for k in ("crv", "kty", "x", "y")}))[:16]
