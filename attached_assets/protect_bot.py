import os, sys, json, hashlib, secrets, base64

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
KEYS_FILE = os.path.join(_dir, "bot_keys.json")
SOURCE_FILE = os.path.join(_dir, "optimized_bot_fixed.py")


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _generate_passwords(master_key: bytes, count: int):
    entries = []
    passwords = []
    for _ in range(count):
        pwd = secrets.token_urlsafe(16)
        salt = os.urandom(16)
        derived = _derive_key(pwd, salt)
        pwd_fernet = Fernet(derived)
        enc_master = pwd_fernet.encrypt(master_key)

        entries.append({
            "id": secrets.token_hex(4),
            "hash": hashlib.sha256(pwd.encode()).hexdigest(),
            "salt": base64.b64encode(salt).decode(),
            "enc_key": base64.b64encode(enc_master).decode(),
            "used": False,
        })
        passwords.append(pwd)
    return entries, passwords


def cmd_encrypt(count):
    with open(SOURCE_FILE, "rb") as f:
        code = f.read()

    master_key = Fernet.generate_key()
    fernet = Fernet(master_key)
    encrypted_code = fernet.encrypt(code)

    with open(ENC_FILE, "wb") as f:
        f.write(encrypted_code)

    entries, passwords = _generate_passwords(master_key, count)

    master_key_backup = base64.b64encode(master_key).decode()
    data = {
        "version": 1,
        "_master_key_backup": master_key_backup,
        "passwords": entries,
    }
    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n{'='*50}")
    print(f"  Bot encrypted -> bot.enc")
    print(f"  Generated {count} passwords")
    print(f"{'='*50}\n")
    for i, p in enumerate(passwords, 1):
        print(f"  {i}. {p}")
    print(f"\n{'='*50}")
    print(f"  DISTRIBUTE: bot.enc, bot_keys.json, run_bot.py,")
    print(f"              camel_engine_wrapper.py, camel_engine.c")
    print(f"  KEEP PRIVATE: protect_bot.py, optimized_bot_fixed.py")
    print(f"{'='*50}\n")


def cmd_add(count):
    if not os.path.exists(KEYS_FILE):
        print("Run 'encrypt' first!")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        data = json.load(f)

    master_key_b64 = data.get("_master_key_backup")
    if not master_key_b64:
        print("Master key backup not found in bot_keys.json!")
        sys.exit(1)

    master_key = base64.b64decode(master_key_b64)
    entries, passwords = _generate_passwords(master_key, count)
    data["passwords"].extend(entries)

    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nAdded {count} new passwords:\n")
    for i, p in enumerate(passwords, 1):
        print(f"  {i}. {p}")


def cmd_list():
    if not os.path.exists(KEYS_FILE):
        print("No keys file found!")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        data = json.load(f)

    active = 0
    used = 0
    print(f"\n{'='*50}")
    for i, p in enumerate(data["passwords"], 1):
        status = "USED" if p["used"] else "ACTIVE"
        marker = "x" if p["used"] else "+"
        if p["used"]:
            used += 1
        else:
            active += 1
        print(f"  [{marker}] #{p['id']} - {status}")
    print(f"{'='*50}")
    print(f"  Active: {active} | Used: {used} | Total: {active + used}")
    print(f"{'='*50}\n")


def cmd_clean():
    if not os.path.exists(KEYS_FILE):
        print("No keys file found!")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        data = json.load(f)

    before = len(data["passwords"])
    data["passwords"] = [p for p in data["passwords"] if not p["used"]]
    after = len(data["passwords"])

    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Removed {before - after} used passwords. {after} remaining.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("""
Usage:
  python protect_bot.py encrypt [N]   - Encrypt bot + generate N passwords (default: 5)
  python protect_bot.py add [N]       - Add N more passwords
  python protect_bot.py list          - Show all passwords status
  python protect_bot.py clean         - Remove used passwords
""")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "encrypt":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_encrypt(n)
    elif cmd == "add":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_add(n)
    elif cmd == "list":
        cmd_list()
    elif cmd == "clean":
        cmd_clean()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
