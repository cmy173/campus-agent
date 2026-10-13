"""Encrypt and decrypt campus credentials using AES-256-GCM.

Security model:
- Master key derived from a fixed device secret + owner ID
- No password prompt; fully automatic on personal devices
- Encrypted blobs stored in SQLite; keys never persisted to disk beyond salt file
"""
import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None


class CredentialCipher:
    """AES-256-GCM cipher for campus credentials."""
    
    def __init__(self, owner_id: str):
        """Initialize with a master key derived from device salt + owner ID.
        
        Args:
            owner_id: User identifier (used to derive unique key per user)
        """
        self.master_key = self._derive_master_key(owner_id)
    
    @staticmethod
    def _derive_master_key(owner_id: str) -> bytes:
        """Derive a 256-bit key from owner ID and a device-specific salt.
        
        The salt is stored in a hidden file in the data directory.
        For personal single-user setup, HMAC-SHA256 provides sufficient security.
        
        Args:
            owner_id: User identifier
            
        Returns:
            32-byte master key
        """
        # Read or create a device-specific salt
        data_dir = Path(__file__).parent.parent / 'data'
        data_dir.mkdir(exist_ok=True)
        salt_file = data_dir / '.device_salt'
        
        if salt_file.exists():
            salt = salt_file.read_bytes()
            if len(salt) != 32:
                # Regenerate if corrupted
                salt = secrets.token_bytes(32)
                salt_file.write_bytes(salt)
                salt_file.chmod(0o600)  # Owner read/write only
        else:
            salt = secrets.token_bytes(32)
            salt_file.write_bytes(salt)
            salt_file.chmod(0o600)
        
        # Derive key using HMAC-SHA256
        return hmac.new(
            salt,
            f'campus-agent:{owner_id}'.encode('utf-8'),
            hashlib.sha256
        ).digest()
    
    def encrypt(self, plaintext: str) -> dict:
        """Encrypt a string and return a dict with nonce + ciphertext.
        
        Args:
            plaintext: String to encrypt (e.g., password)
            
        Returns:
            Dict with keys:
                - nonce: Base64-encoded 12-byte nonce
                - ciphertext: Base64-encoded encrypted data
                - version: Encryption version (for future migrations)
                
        Raises:
            RuntimeError: If cryptography library is not available
        """
        if not AESGCM:
            raise RuntimeError('cryptography library required for encryption')
        
        # Generate random nonce (96 bits recommended for GCM)
        nonce = secrets.token_bytes(12)
        
        # Encrypt using AES-256-GCM
        aesgcm = AESGCM(self.master_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        
        return {
            'nonce': base64.b64encode(nonce).decode('ascii'),
            'ciphertext': base64.b64encode(ciphertext).decode('ascii'),
            'version': 1
        }
    
    def decrypt(self, encrypted: dict) -> str:
        """Decrypt an encrypted dict back to plaintext string.
        
        Args:
            encrypted: Dict from encrypt() method
            
        Returns:
            Decrypted plaintext string
            
        Raises:
            ValueError: If decryption fails (wrong key or corrupted data)
            RuntimeError: If cryptography library is not available
        """
        if not AESGCM:
            raise RuntimeError('cryptography library required for decryption')
        
        try:
            nonce = base64.b64decode(encrypted['nonce'])
            ciphertext = base64.b64decode(encrypted['ciphertext'])
            
            aesgcm = AESGCM(self.master_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            
            return plaintext.decode('utf-8')
        except Exception as e:
            raise ValueError(f'Decryption failed: invalid key or corrupted data') from e


def webvpn_encrypt_password(password: str) -> str:
    """Encrypt password exactly as the WebVPN login page inline script does.

    Reverse-engineered from https://webvpn.zju.edu.cn/login (2026-10-12):
      text = textRightAppend(text, 'utf8')        # pad with '0' chars to multiple of 16
      ct = AesCfb(key, iv, 16).encrypt(text)      # AES-128-CFB, key == iv
      out = hex(iv) + hex(ct)[:len(original)*2]   # hex, truncated to original length

    Args:
        password: Plain text password

    Returns:
        Hex string: 32 chars of IV hex followed by 2*len(password) chars of ciphertext hex

    Raises:
        RuntimeError: If cryptography library is not available
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError('cryptography library required')

    key = b'wrdvpnisawesome!'
    iv = b'wrdvpnisawesome!'

    text = password
    if len(text) % 16 != 0:
        text += '0' * (16 - len(text) % 16)

    cipher = Cipher(algorithms.AES(key), modes.CFB(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(text.encode('utf-8')) + encryptor.finalize()

    return iv.hex() + ciphertext[:len(password)].hex()

