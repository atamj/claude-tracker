"""Génère une paire de clés VAPID pour les Web Push notifications.

Usage:
    python generate_vapid.py [SUBJECT_EMAIL]

Crée :
- vapid_keys.json (clé privée PEM + clé publique base64url + subject)
- vapid_private.pem (clé privée seule, format PEM, utilisée par pywebpush)

Si les fichiers existent déjà, ils ne sont PAS écrasés (sortie 0, message d'info).
"""
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).resolve().parent
KEYS_FILE = HERE / "vapid_keys.json"
PEM_FILE = HERE / "vapid_private.pem"


def generate(subject_email: str) -> None:
    if KEYS_FILE.exists() and PEM_FILE.exists():
        print(f"✓ Clés VAPID déjà présentes ({KEYS_FILE.name}) — non écrasées.")
        return

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    pub_numbers = priv.public_key().public_numbers()
    pub_raw = b"\x04" + pub_numbers.x.to_bytes(32, "big") + pub_numbers.y.to_bytes(32, "big")
    pub_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()

    priv_raw = priv.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_raw).rstrip(b"=").decode()

    subject = subject_email.strip()
    if not subject.startswith("mailto:") and "@" in subject:
        subject = f"mailto:{subject}"

    KEYS_FILE.write_text(json.dumps({
        "private_pem": priv_pem,
        "private_b64": priv_b64,
        "public_b64": pub_b64,
        "subject": subject,
    }, indent=2))
    KEYS_FILE.chmod(0o600)

    PEM_FILE.write_text(priv_pem)
    PEM_FILE.chmod(0o600)

    print(f"✓ Clés VAPID générées :")
    print(f"  - {KEYS_FILE}")
    print(f"  - {PEM_FILE}")
    print(f"  Subject : {subject}")
    print(f"  Public  : {pub_b64[:40]}...")


if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "mailto:admin@example.com"
    generate(subject)
