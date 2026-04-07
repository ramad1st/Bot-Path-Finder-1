import os, sys, hashlib, secrets, base64

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
MASTER_FILE = os.path.join(_dir, ".master.key")
SOURCE_FILE = os.path.join(_dir, "optimized_bot_fixed.py")


def _make_token(master_key: bytes) -> str:
    nonce = os.urandom(16)
    mask = hashlib.pbkdf2_hmac("sha256", nonce, b"camelbot_mask", 1, dklen=len(master_key))
    xored = bytes(a ^ b for a, b in zip(master_key, mask))
    tag = hashlib.sha256(nonce + master_key + b"camelbot_verify").digest()[:8]
    raw = nonce + xored + tag
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


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

    tokens = [_make_token(master_key) for _ in range(count)]

    print(f"\n{'='*50}")
    print(f"  Bot encrypted -> bot.enc")
    print(f"  Generated {count} tokens")
    print(f"{'='*50}\n")
    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t}")
    print(f"\n{'='*50}")
    print(f"  DISTRIBUTE: bot.enc, run_bot.py,")
    print(f"              camel_engine_wrapper.py, camel_engine.c")
    print(f"  KEEP PRIVATE: protect_bot.py, .master.key,")
    print(f"                optimized_bot_fixed.py")
    print(f"{'='*50}\n")


def cmd_add(count):
    if not os.path.exists(MASTER_FILE):
        print("Run 'encrypt' first!")
        sys.exit(1)

    with open(MASTER_FILE, "rb") as f:
        master_key = f.read()

    tokens = [_make_token(master_key) for _ in range(count)]

    print(f"\n  {count} new tokens:\n")
    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("""
Usage:
  python protect_bot.py encrypt [N]  - Encrypt bot + generate N tokens (default: 5)
  python protect_bot.py add [N]      - Generate N more tokens
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
