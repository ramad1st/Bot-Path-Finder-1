import os, sys, json, hashlib, base64, getpass, time, signal, subprocess, threading, atexit

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
SESSION_MINUTES = 30


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _secure_delete(path):
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, "wb") as f:
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
            os.unlink(path)
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass


def _validate_password(password):
    if not os.path.exists(KEYS_FILE):
        return None, "bot_keys.json not found"

    with open(KEYS_FILE) as f:
        data = json.load(f)

    pwd_hash = hashlib.sha256(password.encode()).hexdigest()

    for entry in data["passwords"]:
        if entry["hash"] == pwd_hash:
            if entry["used"]:
                return None, "PASSWORD_USED"
            entry["used"] = True
            with open(KEYS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            return entry, None

    return None, "INVALID"


def _decrypt_bot(entry, password):
    salt = base64.b64decode(entry["salt"])
    derived = _derive_key(password, salt)

    pwd_fernet = Fernet(derived)
    master_key = pwd_fernet.decrypt(base64.b64decode(entry["enc_key"]))

    with open(ENC_FILE, "rb") as f:
        encrypted_code = f.read()

    bot_fernet = Fernet(master_key)
    return bot_fernet.decrypt(encrypted_code)


def main():
    if not os.path.exists(ENC_FILE):
        print("bot.enc not found!")
        sys.exit(1)

    print(f"\n{'='*40}")
    print(f"  CamelBot Protected Launcher")
    print(f"  Session: {SESSION_MINUTES} minutes")
    print(f"{'='*40}\n")

    password = getpass.getpass("Password: ")

    entry, error = _validate_password(password)

    if error == "PASSWORD_USED":
        print("\nThis password has already been used!")
        sys.exit(1)
    elif error == "INVALID":
        print("\nInvalid password!")
        sys.exit(1)
    elif error:
        print(f"\nError: {error}")
        sys.exit(1)

    try:
        code = _decrypt_bot(entry, password)
    except Exception:
        print("\nDecryption failed!")
        sys.exit(1)

    tmp_name = f".~_bot_{os.getpid()}.py"
    tmp_path = os.path.join(_dir, tmp_name)

    with open(tmp_path, "wb") as f:
        f.write(code)
    os.chmod(tmp_path, 0o600)

    atexit.register(_secure_delete, tmp_path)

    extra_args = sys.argv[1:]
    mitm_cmd = ["mitmdump", "-s", tmp_path] + extra_args

    print(f"\nStarting bot... (auto-stop in {SESSION_MINUTES} min)\n")

    proc = subprocess.Popen(mitm_cmd)

    def kill_timer():
        time.sleep(SESSION_MINUTES * 60)
        print(f"\n\nSession expired ({SESSION_MINUTES} minutes)!")
        proc.terminate()
        time.sleep(3)
        if proc.poll() is None:
            proc.kill()
        _secure_delete(tmp_path)

    timer = threading.Thread(target=kill_timer, daemon=True)
    timer.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        _secure_delete(tmp_path)

    print("\nBot stopped.")


if __name__ == "__main__":
    main()
