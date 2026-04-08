import os, sys, hashlib, base64, getpass, time, subprocess, threading, atexit

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
USED_FILE = os.path.join(_dir, ".bot_used")
SESSION_MINUTES = 30

_SHM = "/dev/shm"
_USE_RAM = os.path.isdir(_SHM)


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
    if _USE_RAM:
        print(f"  Mode: RAM-only (no disk write)")
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

    rand_id = hashlib.md5(os.urandom(16)).hexdigest()[:8]
    if _USE_RAM:
        tmp_path = os.path.join(_SHM, f".cb_{rand_id}.py")
    else:
        tmp_path = os.path.join(_dir, f".~_bot_{rand_id}.py")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, code)
    os.close(fd)

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
