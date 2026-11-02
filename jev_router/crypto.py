import base64
import hashlib
import os
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes, source: Literal["env", "file"]):
        self._fernet = Fernet(key)
        self.source = source

    @classmethod
    def from_settings(cls, secret: str, key_file: str | Path) -> "SecretBox":
        if secret:
            return cls(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()), "env")
        path = Path(key_file)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
        return cls(path.read_bytes().strip(), "file")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None


def mask(value: str) -> str:
    return f"{value[:3]}…{value[-4:]}" if len(value) >= 12 else f"…{value[-2:]}"
