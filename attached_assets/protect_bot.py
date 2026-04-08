import os, sys, json, hashlib, string, secrets

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
MASTER_FILE = os.path.join(_dir, ".master.key")
KEYS_FILE = os.path.join(_dir, "bot_keys.json")
SOURCE_FILE = os.path.join(_dir, "optimized_bot_fixed.py")

_CHARS = string.ascii_uppercase + string.digits


def _make_code():
    parts = []
    for _ in range(3):
        parts.append("".join(secrets.choice(_CHARS) for _ in range(4)))
    return "-".join(parts)


def _code_hash(code: str) -> str:
    clean = code.strip().upper().replace("-", "")
    return hashlib.sha256(clean.encode()).hexdigest()


def _encrypt_master_key(master_key: bytes, code: str) -> str:
    clean = code.strip().upper().replace("-", "")
    dk = hashlib.pbkdf2_hmac("sha256", clean.encode(), b"camelbot_dk", 100000)
    f = Fernet(Fernet.generate_key())
    salt = os.urandom(16)
    real_dk = hashlib.pbkdf2_hmac("sha256", clean.encode(), salt, 100000)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(real_dk), modes.CFB(iv), backend=default_backend())
    enc = cipher.encryptor()
    ct = enc.update(master_key) + enc.finalize()
    import base64
    blob = base64.b64encode(salt + iv + ct).decode()
    return blob


def _load_keys():
    if os.path.exists(KEYS_FILE):
        with open(KEYS_FILE) as f:
            return json.load(f)
    return {}


def _save_keys(keys):
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)


def cmd_encrypt(count):
    with open(SOURCE_FILE, "rb") as f:
        code = f.read()

    master_key = Fernet.generate_key()
    fernet = Fernet(master_key)
    encrypted_code = fernet.encrypt(code)

    with open(ENC_FILE, "wb") as f:
        f.write(encrypted_code)

    with open(MASTER_FILE, "wb") as f:
        f.write(master_key)

    keys = {}
    codes = []
    for _ in range(count):
        c = _make_code()
        h = _code_hash(c)
        blob = _encrypt_master_key(master_key, c)
        keys[h] = blob
        codes.append(c)

    _save_keys(keys)

    print(f"\n{'='*40}")
    print(f"  Bot encrypted -> bot.enc")
    print(f"  Generated {count} codes")
    print(f"{'='*40}\n")
    for i, c in enumerate(codes, 1):
        print(f"  {i}. {c}")
    print(f"\n{'='*40}")
    print(f"  DISTRIBUTE: bot.enc, bot_keys.json,")
    print(f"    run_bot.py, camel_engine_wrapper.py,")
    print(f"    camel_engine.c, config.json")
    print(f"  KEEP PRIVATE: protect_bot.py,")
    print(f"    .master.key, optimized_bot_fixed.py")
    print(f"{'='*40}\n")


def cmd_add(count):
    if not os.path.exists(MASTER_FILE):
        print("Run 'encrypt' first!")
        sys.exit(1)

    with open(MASTER_FILE, "rb") as f:
        master_key = f.read()

    keys = _load_keys()
    codes = []
    for _ in range(count):
        c = _make_code()
        h = _code_hash(c)
        blob = _encrypt_master_key(master_key, c)
        keys[h] = blob
        codes.append(c)

    _save_keys(keys)

    print(f"\n  {count} new codes:\n")
    for i, c in enumerate(codes, 1):
        print(f"  {i}. {c}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("""
Usage:
  python protect_bot.py encrypt [N]  - Encrypt bot + generate N codes (default: 5)
  python protect_bot.py add [N]      - Generate N more codes
""")
        sys.exit(1)

    cmd = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if cmd == "encrypt":
        cmd_encrypt(n)
    elif cmd == "add":
        cmd_add(n)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
