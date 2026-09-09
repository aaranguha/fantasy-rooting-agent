#!/usr/bin/env python3
"""Pull your own ESPN fantasy cookies out of your local browser and write them
to ~/.fantasy-agent/.env, so you never have to dig through DevTools.

    python scripts/grab_espn_cookies.py            # auto-detect browser
    python scripts/grab_espn_cookies.py --browser chrome
    python scripts/grab_espn_cookies.py --print    # show masked values only
    python scripts/grab_espn_cookies.py --dry-run  # find them, write nothing

Nothing is ever printed in full: the script writes espn_s2 / SWID straight into
the chmod-600 .env and reports only masked previews. It reads only cookies whose
host contains "espn.com".

macOS notes:
  * Chromium browsers encrypt cookies with a key in your login Keychain, so the
    OS will pop up a password prompt the first time. Click "Always Allow".
  * Safari's cookie jar requires Full Disk Access for your terminal
    (System Settings > Privacy & Security > Full Disk Access).
  * Firefox needs neither.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
WANTED = ("espn_s2", "SWID")

# name -> (cookie db path, keychain service, keychain account)
CHROMIUM = {
    "chrome": (HOME / "Library/Application Support/Google/Chrome",
               "Chrome Safe Storage", "Chrome"),
    "brave": (HOME / "Library/Application Support/BraveSoftware/Brave-Browser",
              "Brave Safe Storage", "Brave"),
    "edge": (HOME / "Library/Application Support/Microsoft Edge",
             "Microsoft Edge Safe Storage", "Microsoft Edge"),
    "arc": (HOME / "Library/Application Support/Arc/User Data",
            "Arc Safe Storage", "Arc"),
    "vivaldi": (HOME / "Library/Application Support/Vivaldi",
                "Vivaldi Safe Storage", "Vivaldi"),
}
FIREFOX_ROOT = HOME / "Library/Application Support/Firefox/Profiles"
SAFARI_COOKIES = HOME / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 12:
        return value[:2] + "…" + value[-2:]
    return f"{value[:6]}…{value[-4:]}  ({len(value)} chars)"


# ---------------------------------------------------------------------------
# Chromium
# ---------------------------------------------------------------------------

def chromium_key(service: str, account: str) -> bytes:
    """Derive the AES key from the browser's Keychain password."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Keychain prompt timed out - approve the dialog and retry")
    if out.returncode != 0:
        raise RuntimeError(
            f"Keychain has no '{service}' entry (is {account} installed and run at least once?)")
    import hashlib

    password = out.stdout.strip().encode()
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def chromium_decrypt(blob: bytes, key: bytes) -> str:
    if not blob:
        return ""
    if not blob.startswith(b"v10"):
        return blob.decode("utf-8", "replace")   # older/unencrypted
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError(
            "Chromium cookies are AES-encrypted. Install the decoder first:\n"
            "    pip install cryptography")

    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    dec = cipher.decryptor()
    plain = dec.update(blob[3:]) + dec.finalize()
    if plain:                                     # strip PKCS7 padding
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
    # Chrome 130+ prefixes the plaintext with a 32-byte SHA256 of the domain.
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return plain[32:].decode("utf-8", "replace")


def cookie_dbs(root: Path) -> list[Path]:
    found = []
    for profile in ("Default", *(p.name for p in root.glob("Profile *") if p.is_dir())):
        for rel in ("Cookies", "Network/Cookies"):
            db = root / profile / rel
            if db.exists():
                found.append(db)
    return found


def read_chromium(name: str) -> dict[str, str]:
    root, service, account = CHROMIUM[name]
    dbs = cookie_dbs(root)
    if not dbs:
        raise FileNotFoundError(f"No {name} cookie database under {root}")
    key = chromium_key(service, account)

    out: dict[str, str] = {}
    for db in dbs:
        # Copy first: the live file is locked while the browser is running.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "cookies.sqlite"
            shutil.copy2(db, copy)
            for extra in (".sqlite-wal", "-wal", "-journal"):
                sidecar = Path(str(db) + extra)
                if sidecar.exists():
                    shutil.copy2(sidecar, str(copy) + extra)
            con = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
            try:
                rows = con.execute(
                    "SELECT name, value, encrypted_value, host_key FROM cookies "
                    "WHERE host_key LIKE '%espn.com'").fetchall()
            finally:
                con.close()
        for cname, plain, enc, host in rows:
            if cname not in WANTED:
                continue
            value = plain or chromium_decrypt(enc, key)
            if value and (cname not in out or len(value) > len(out[cname])):
                out[cname] = value
    return out


# ---------------------------------------------------------------------------
# Firefox
# ---------------------------------------------------------------------------

def read_firefox() -> dict[str, str]:
    profiles = sorted(FIREFOX_ROOT.glob("*/cookies.sqlite"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if not profiles:
        raise FileNotFoundError(f"No Firefox cookies.sqlite under {FIREFOX_ROOT}")
    out: dict[str, str] = {}
    for db in profiles:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "cookies.sqlite"
            shutil.copy2(db, copy)
            con = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
            try:
                rows = con.execute(
                    "SELECT name, value FROM moz_cookies WHERE host LIKE '%espn.com'").fetchall()
            finally:
                con.close()
        for cname, value in rows:
            if cname in WANTED and value:
                out.setdefault(cname, value)
        if len(out) == len(WANTED):
            break
    return out


# ---------------------------------------------------------------------------
# Safari (Cookies.binarycookies)
# ---------------------------------------------------------------------------

def read_safari() -> dict[str, str]:
    import struct

    if not SAFARI_COOKIES.exists():
        raise FileNotFoundError(
            f"{SAFARI_COOKIES} not readable. Grant your terminal Full Disk Access in "
            "System Settings > Privacy & Security > Full Disk Access, then reopen it.")
    data = SAFARI_COOKIES.read_bytes()
    if data[:4] != b"cook":
        raise ValueError("Not a Safari binarycookies file")

    out: dict[str, str] = {}
    count = struct.unpack(">i", data[4:8])[0]
    sizes = struct.unpack(f">{count}i", data[8:8 + count * 4])
    offset = 8 + count * 4

    for size in sizes:
        page = data[offset:offset + size]
        offset += size
        if page[:4] != b"\x00\x00\x01\x00":
            continue
        n = struct.unpack("<i", page[4:8])[0]
        starts = struct.unpack(f"<{n}i", page[8:8 + n * 4])
        for start in starts:
            try:
                url_off, name_off, path_off, val_off = struct.unpack(
                    "<iiii", page[start + 16:start + 32])
                def cstr(o: int) -> str:
                    end = page.index(b"\x00", start + o)
                    return page[start + o:end].decode("utf-8", "replace")
                host, cname, value = cstr(url_off), cstr(name_off), cstr(val_off)
            except (struct.error, ValueError):
                continue
            if "espn.com" in host and cname in WANTED and value:
                out.setdefault(cname, value)
    return out


# ---------------------------------------------------------------------------

READERS = {name: (lambda n=name: read_chromium(n)) for name in CHROMIUM}
READERS["firefox"] = read_firefox
READERS["safari"] = read_safari


def agent_home() -> Path:
    env = os.getenv("FANTASY_AGENT_HOME")
    path = Path(env).expanduser() if env else HOME / ".fantasy-agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_env(values: dict[str, str]) -> Path:
    path = agent_home() / ".env"
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing.update(values)
    body = ["# Secrets - never commit this file.", ""]
    body += [f"{k}={v}" for k, v in sorted(existing.items())]
    path.write_text("\n".join(body) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", choices=sorted(READERS), help="default: try all")
    ap.add_argument("--dry-run", action="store_true", help="find them but write nothing")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print masked previews (never the full value)")
    args = ap.parse_args()

    order = [args.browser] if args.browser else [
        "chrome", "brave", "edge", "arc", "vivaldi", "safari", "firefox"]

    found: dict[str, str] = {}
    problems: list[str] = []
    for name in order:
        try:
            got = READERS[name]()
        except FileNotFoundError:
            continue                        # browser simply isn't installed
        except Exception as exc:            # noqa: BLE001
            problems.append(f"  {name}: {exc}")
            continue
        if got.get("espn_s2") and got.get("SWID"):
            print(f"✓ Found both ESPN cookies in {name}")
            found = got
            break
        if got:
            problems.append(f"  {name}: only found {', '.join(sorted(got))}")

    if not found:
        print("✗ Could not find espn_s2 + SWID in any browser.\n")
        if problems:
            print("What happened:")
            print("\n".join(problems) + "\n")
        print("Most likely you're not signed in to fantasy.espn.com in that browser.\n"
              "Sign in, load your league page once, then re-run this script.")
        return 1

    swid = found["SWID"].strip()
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"

    if args.show or args.dry_run:
        print(f"  espn_s2  {mask(found['espn_s2'])}")
        print(f"  SWID     {mask(swid)}")
    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0

    path = write_env({"ESPN_S2": found["espn_s2"], "ESPN_SWID": swid})
    print(f"✓ Wrote ESPN_S2 and ESPN_SWID to {path} (chmod 600)\n")
    print("Next:  fantasy-agent setup     # answer 'n' when it offers to take cookies")
    print("       fantasy-agent leagues   # confirm both ESPN leagues load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
