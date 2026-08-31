"""
This is what would live INSIDE the FaithView Pro client, not on the server.
It proves the offline verification loop works: given only server_public.pem
and a token string, it checks the signature and expiry with zero network
calls. Run it after activating a device to see it in action.

Usage:
    python verify_token.py "<token string from /activate response>"
"""
import sys
import base64
import json
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_KEY_PATH = os.path.join(BASE_DIR, "server_public.pem")


def load_public_key():
    with open(PUBLIC_KEY_PATH, "rb") as f:
        return serialization.load_pem_public_key(f.read())


def verify(token: str) -> dict:
    payload_b64, sig_b64 = token.split(".")
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    signature = base64.urlsafe_b64decode(sig_b64)

    public_key = load_public_key()
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature:
        raise ValueError("INVALID: signature does not match — token was tampered with or forged")

    payload = json.loads(payload_bytes)
    expires_at = datetime.fromisoformat(payload["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        raise ValueError(f"EXPIRED: token expired at {payload['expires_at']}")

    return payload


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python verify_token.py <token>")
        sys.exit(1)

    try:
        payload = verify(sys.argv[1])
        print("VALID offline token")
        print(json.dumps(payload, indent=2))
    except Exception as e:
        print(f"REJECTED: {e}")
        sys.exit(1)
