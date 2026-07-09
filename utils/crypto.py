"""
utils/crypto.py
===============
CredentialVault — encrypt/decrypt API keys using Fernet (AES-256).
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

logger = logging.getLogger("utils.crypto")


class CredentialVault:
    """Encrypt/decrypt API keys stored in the database using Fernet."""

    def __init__(self, key: Optional[str] = None):
        self._fernet = None
        if key:
            self._init_fernet(key)

    def _init_fernet(self, key: str) -> None:
        from cryptography.fernet import Fernet
        key_bytes = key.encode("utf-8").ljust(32, b"\0")[:32]
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        self._fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        if not self._fernet or not plaintext:
            return plaintext or ""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not self._fernet or not ciphertext:
            return ciphertext or ""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except Exception:
            logger.error("Decryption failed — returning empty string")
            return ""