"""Official WebVPN browser wire encryption; no stored credential cipher."""
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

