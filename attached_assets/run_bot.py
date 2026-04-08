import os, sys, json, hashlib, base64, getpass, time, subprocess, threading, atexit

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("Run: pip install cryptography")
    sys.exit(1)

_dir = os.path.dirname(os.path.abspath(__file__))
ENC_FILE = os.path.join(_dir, "bot.enc")
KEYS_FILE = os.path.join(_dir, "bot_keys.json")
USED_FILE = os.path.join(_dir, ".bot_used")
SESSION_MINUTES = 30

_SHM = "/dev/shm"
_USE_RAM = os.path.isdir(_SHM)


def _code_hash(code: str) -> str:
    clean = code.strip().upper().replace("-", "")
    return hashlib.sha256(clean.encode()).hexdigest()


def _decrypt_master_key(blob: str, code: str) -> bytes:
    clean = code.strip().upper().replace("-", "")
    raw = base64.b64decode(blob)
    salt = raw[:16]
    iv = raw[16:32]
    ct = raw[32:]
    dk = hashlib.pbkdf2_hmac("sha256", clean.encode(), salt, 100000)
    cipher = Cipher(algorithms.AES(dk), modes.CFB(iv), backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(ct) + dec.finalize()


def _is_used(code_hash: str) -> bool:
    if not os.path.exists(USED_FILE):
        return False
    with open(USED_FILE) as f:
        return code_hash in f.read()


def _mark_used(code_hash: str):
    with open(USED_FILE, "a") as f:
        f.write(code_hash + "\n")


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

    if not os.path.exists(KEYS_FILE):
        print("bot_keys.json not found!")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        keys = json.load(f)

    print(f"\n{'='*40}")
    print(f"  CamelBot Protected Launcher")
    print(f"  Session: {SESSION_MINUTES} minutes")
    print(f"{'='*40}\n")

    code = input("Code: ").strip().upper()

    ch = _code_hash(code)

    if _is_used(ch):
        print("\nThis code has already been used!")
        sys.exit(1)

    if ch not in keys:
        print("\nInvalid code!")
        sys.exit(1)

    try:
        master_key = _decrypt_master_key(keys[ch], code)
        with open(ENC_FILE, "rb") as f:
            encrypted_code = f.read()
        fernet = Fernet(master_key)
        bot_code = fernet.decrypt(encrypted_code)
    except Exception:
        print("\nDecryption failed!")
        sys.exit(1)

    _mark_used(ch)

    rand_id = hashlib.md5(os.urandom(16)).hexdigest()[:8]
    if _USE_RAM:
        tmp_path = os.path.join(_SHM, f".cb_{rand_id}.py")
    else:
        tmp_path = os.path.join(_dir, f".~_bot_{rand_id}.py")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, bot_code)
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
