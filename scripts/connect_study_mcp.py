#!/usr/bin/env python3
"""User-run secure setup for the prepared read-only Study Library MCP tunnel.

Create the tunnel and a restricted runtime key in the official Platform UI first.
The key is entered without echo and is never placed in shell arguments or reports.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

BASE = Path(__file__).resolve().parents[1]
CLIENT = BASE / "runtime/study-mcp/bin/tunnel-client"
PYTHON = BASE / ".venv-study-mcp/bin/python"


def status_fields(value):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"process_running", "healthy", "ready"} and isinstance(item, bool):
                result[key] = item
            elif isinstance(item, (dict, list)):
                result.update(status_fields(item))
    elif isinstance(value, list):
        for item in value:
            result.update(status_fields(item))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel-id")
    parser.add_argument("--key-file", type=Path, help="Existing private file containing a Tunnels Read + Use runtime key")
    args = parser.parse_args()
    tunnel_id = (args.tunnel_id or input("Official tunnel ID (tunnel_...): ")).strip()
    if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]{8,128}", tunnel_id):
        raise SystemExit("Invalid tunnel ID; no connection created.")
    if not CLIENT.is_file() or not PYTHON.is_file():
        raise SystemExit("Prepared runtime is missing; return to Codex before entering a key.")
    key_file = args.key_file
    if key_file is None:
        secret_dir = Path.home() / ".config/study-pro-bridge/secrets"
        if any(p.is_symlink() for p in [secret_dir, *secret_dir.parents]):
            raise SystemExit("Secret directory must not use symlinks.")
        secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        secret_dir.chmod(0o700)
        key_file = secret_dir / "study-library-tunnel.key"
        if not key_file.exists():
            key = getpass.getpass("Restricted Tunnels Read + Use runtime key (hidden; not a model API call): ").strip()
            if not 20 <= len(key) <= 1024 or any(c.isspace() for c in key) or key.startswith("sk-admin"):
                raise SystemExit("A restricted runtime key is required; no connection created.")
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(key + "\n")
            del key
    key_file = key_file.absolute()
    if key_file.is_symlink() or not key_file.is_file():
        raise SystemExit("Key file must be a regular private file.")
    info = key_file.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("Key file must be owned by this user with mode 600 or stricter.")
    command = shlex.join([str(PYTHON), str(BASE / "study_mcp.py")])
    argv = [str(CLIENT), "runtimes", "connect", "--alias", "study-library", "--tunnel-id", tunnel_id,
            "--profile", "study-library", "--profile-dir", str(BASE / "runtime/study-mcp/profiles"),
            "--runtime-api-key", "file:" + str(key_file), "--mcp-command", command, "--json"]
    connected = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if connected.returncode:
        # Raw tool stderr is deliberately not printed: credentials belong only
        # to the official client and the private secret file.
        raise SystemExit("Tunnel connection failed. Keep the key private; inspect the managed runtime status with Codex.")
    checked = subprocess.run([str(CLIENT), "runtimes", "status", "study-library", "--json"],
                             capture_output=True, text=True, timeout=30)
    try:
        fields = status_fields(json.loads(checked.stdout))
    except (ValueError, TypeError):
        fields = {}
    print(json.dumps({"alias": "study-library", "tunnel_id": tunnel_id, **fields,
                      "web_chat_verified": False}, ensure_ascii=False))
    if checked.returncode or not all(fields.get(k) is True for k in ("process_running", "healthy", "ready")):
        raise SystemExit("Runtime is not yet fully ready; do not mark the ChatGPT connection complete.")
    print("Local tunnel is ready. Create/enable the Study Library custom app in ChatGPT, then verify it with GPT-5.6 medium.")


if __name__ == "__main__":
    main()
