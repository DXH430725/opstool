"""Fernet-encrypted credential vault with PBKDF2 key derivation."""

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

VAULT_DIR = Path(__file__).resolve().parent.parent / "data"
VAULT_FILE = VAULT_DIR / "vault.enc"
SALT_LEN = 16
PBKDF2_ITERATIONS = 600_000


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


class Vault:
    def __init__(self, passphrase: str | None = None):
        self._passphrase = passphrase or os.environ.get("OPS_MASTER_KEY", "")
        if not self._passphrase:
            raise RuntimeError("No master key: set OPS_MASTER_KEY env var")
        self._data: dict = {}
        self._salt: bytes = b""
        if VAULT_FILE.exists():
            self._load()

    def _load(self):
        raw = VAULT_FILE.read_bytes()
        self._salt = raw[:SALT_LEN]
        token = raw[SALT_LEN:]
        key = _derive_key(self._passphrase, self._salt)
        try:
            plaintext = Fernet(key).decrypt(token)
        except InvalidToken:
            raise RuntimeError("Vault decryption failed — wrong master key")
        self._data = json.loads(plaintext)

    def save(self):
        if not self._salt:
            self._salt = os.urandom(SALT_LEN)
        key = _derive_key(self._passphrase, self._salt)
        token = Fernet(key).encrypt(json.dumps(self._data).encode())
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        VAULT_FILE.write_bytes(self._salt + token)

    def set(
        self,
        name: str,
        ip: str,
        user: str,
        password: str = "",
        port: int = 22,
        key_path: str | None = None,
    ):
        entry = {"ip": ip, "user": user, "password": password, "port": port}
        if key_path:
            entry["key_path"] = key_path
        self._data[name] = entry
        self.save()

    def get(self, name: str) -> dict:
        entry = self._data.get(name)
        if not entry:
            raise KeyError(f"Server '{name}' not in vault")
        return dict(entry)

    def remove(self, name: str):
        if name not in self._data:
            raise KeyError(f"Server '{name}' not in vault")
        del self._data[name]
        self.save()

    def list_servers(self) -> list[str]:
        return list(self._data.keys())
