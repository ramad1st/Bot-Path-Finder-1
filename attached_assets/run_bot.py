import os, sys, hashlib, base64, getpass, time, subprocess, threading, atexit

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
USED_FILE = os.path.join(_dir, ".bot_used")
_CFG_FILE = os.path.join(_dir, "config.json")

def _load_session_minutes():
    import json
    try:
        with open(_CFG_FILE) as f:
            return json.load(f).get("session_minutes", 30)
    except Exception:
        return 30

SESSION_MINUTES = _load_session_minutes()


def _extract_master_key(token: str):
    padding = 4 - len(token) % 4
    if padding != 4:
        token += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(token)
    except Exception:
        return None

    if len(raw) < 25:
        return None

    nonce = raw[:16]
    xored = raw[16:-8]
    tag = raw[-8:]

    mask = hashlib.pbkdf2_hmac("sha256", nonce, b"camelbot_mask", 1, dklen=len(xored))
    master_key = bytes(a ^ b for a, b in zip(xored, mask))

    expected_tag = hashlib.sha256(nonce + master_key + b"camelbot_verify").digest()[:8]
    if tag != expected_tag:
        return None
    return master_key


def _is_used(token: str) -> bool:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not os.path.exists(USED_FILE):
        return False
    with open(USED_FILE) as f:
        return token_hash in f.read()


def _mark_used(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with open(USED_FILE, "a") as f:
        f.write(token_hash + "\n")


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


def main():
    if not os.path.exists(ENC_FILE):
        print("bot.enc not found!")
        sys.exit(1)

    print(f"\n{'='*40}")
    print(f"  CamelBot Protected Launcher")
    print(f"  Session: {SESSION_MINUTES} minutes")
    print(f"{'='*40}\n")

    token = getpass.getpass("Token: ")

    if _is_used(token):
        print("\nThis token has already been used!")
        sys.exit(1)

    master_key = _extract_master_key(token)
    if master_key is None:
        print("\nInvalid token!")
        sys.exit(1)

    try:
        with open(ENC_FILE, "rb") as f:
            encrypted_code = f.read()
        fernet = Fernet(master_key)
        code = fernet.decrypt(encrypted_code)
    except Exception:
        print("\nDecryption failed!")
        sys.exit(1)

    _mark_used(token)

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
