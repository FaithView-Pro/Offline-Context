"""
Run this ONCE to generate the server's Ed25519 keypair.

server_private.pem -> stays on your license server ONLY. Never ships in the app.
server_public.pem  -> gets embedded in the FaithView Pro client for OFFLINE
                       token verification (no network call needed at runtime).
"""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import os

OUT = os.path.dirname(os.path.abspath(__file__))

private_key = Ed25519PrivateKey.generate()
public_key = private_key.public_key()

priv_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
pub_bytes = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)

with open(os.path.join(OUT, "server_private.pem"), "wb") as f:
    f.write(priv_bytes)
with open(os.path.join(OUT, "server_public.pem"), "wb") as f:
    f.write(pub_bytes)

print("Generated server_private.pem and server_public.pem")
print("Keep server_private.pem OFF any machine you ship to customers.")
