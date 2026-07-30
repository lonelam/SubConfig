#!/usr/bin/env python3
"""Interactive tool to manage SubConfig's server-hosted secrets.

Subscribe URLs and self-hosted proxy definitions (e.g. your own
xray-Reality node) are no longer stored as GitHub Secrets. Instead they
live in two files on your own server, at the same host used for
deployment (secrets.HOST in the GitHub Actions workflow):

    <remote-dir>/subscribe.txt              one subscribe URL per line
    <remote-dir>/self-hosted-proxies.yaml   YAML list of proxy entries

This script connects to that server over SSH, lets you view/add/remove
entries interactively, and writes the changes back.

Requirements:
    - `ssh`/`scp` available locally, already configured for passwordless
      login to the server (e.g. via ssh-agent, or an IdentityFile/User
      entry in ~/.ssh/config for the host). This script never asks for
      or handles a private key path/passphrase itself.
    - PyYAML (`pip install pyyaml`).

Usage:
    python3 manage_secrets.py
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field

try:
    import yaml
except ImportError:
    print("PyYAML is required. Install it with: pip install pyyaml")
    sys.exit(1)

DEFAULT_PORT = "22"
DEFAULT_USER = "www"
DEFAULT_REMOTE_DIR = "/www/private/secrets"
SUBSCRIBE_FILE = "subscribe.txt"
PROXIES_FILE = "self-hosted-proxies.yaml"

REALITY_DEFAULTS = {
    "type": "vless",
    "port": "443",
    "network": "tcp",
    "flow": "xtls-rprx-vision",
    "servername": "www.microsoft.com",
    "client-fingerprint": "chrome",
}


def prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{msg}{suffix}: ").strip()
    return value or default


def mask_middle(s: str, keep: int = 6) -> str:
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}"


@dataclass
class Remote:
    host: str
    port: str
    user: str
    remote_dir: str

    def _ssh_base(self) -> list[str]:
        return ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-p", self.port, f"{self.user}@{self.host}"]

    def read(self, filename: str) -> str:
        remote_path = f"{self.remote_dir}/{filename}"
        cmd = self._ssh_base() + [f"cat {shlex.quote(remote_path)} 2>/dev/null || true"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to read {remote_path}: {result.stderr.strip()}")
        return result.stdout

    def write(self, filename: str, content: str) -> None:
        remote_path = f"{self.remote_dir}/{filename}"
        mkdir_cmd = self._ssh_base() + [f"mkdir -p {shlex.quote(self.remote_dir)}"]
        subprocess.run(mkdir_cmd, check=True)
        write_cmd = self._ssh_base() + [
            f"cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}"
        ]
        subprocess.run(write_cmd, input=content, text=True, check=True)


def load_subscribe(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def dump_subscribe(urls: list[str]) -> str:
    return "".join(f"{u}\n" for u in urls)


def load_proxies(raw: str) -> list[dict]:
    raw = raw.strip()
    if not raw:
        return []
    data = yaml.safe_load(raw)
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError("self-hosted-proxies.yaml must contain a YAML list")
    return data


def dump_proxies(proxies: list[dict]) -> str:
    if not proxies:
        return ""
    return yaml.dump(proxies, allow_unicode=True, sort_keys=False, default_flow_style=False)


def show_subscribe(urls: list[str]) -> None:
    if not urls:
        print("  (no subscribe URLs configured)")
        return
    for i, u in enumerate(urls, 1):
        print(f"  {i}. {mask_middle(u)}")


def show_proxies(proxies: list[dict]) -> None:
    if not proxies:
        print("  (no self-hosted proxies configured)")
        return
    for i, p in enumerate(proxies, 1):
        print(f"  {i}. {p.get('name')}  ({p.get('type')} @ {mask_middle(str(p.get('server', '')))})")


def add_subscribe(urls: list[str]) -> bool:
    url = prompt("Subscribe URL to add (http(s):// or tg://)")
    if not url:
        print("Cancelled.")
        return False
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        print("URL must start with http://, https:// or tg://. Not added.")
        return False
    urls.append(url)
    print("Added (not yet saved to server).")
    return True


def remove_subscribe(urls: list[str]) -> bool:
    show_subscribe(urls)
    if not urls:
        return False
    idx = prompt("Index to remove (blank to cancel)")
    if not idx.isdigit() or not (1 <= int(idx) <= len(urls)):
        print("Cancelled.")
        return False
    removed = urls.pop(int(idx) - 1)
    print(f"Removed: {mask_middle(removed)}")
    return True


def add_proxy_reality_wizard() -> dict | None:
    print("\n-- Add a VLESS + REALITY (xray-reality) node --")
    name = prompt("Display name", "🏠 自建节点")
    server = prompt("Server address (domain or IP)")
    if not server:
        print("Server is required. Cancelled.")
        return None
    uuid = prompt("UUID")
    if not uuid:
        print("UUID is required. Cancelled.")
        return None
    public_key = prompt("Reality public-key")
    if not public_key:
        print("public-key is required. Cancelled.")
        return None

    port = prompt("Port", REALITY_DEFAULTS["port"])
    network = prompt("Network", REALITY_DEFAULTS["network"])
    flow = prompt("Flow (blank for none)", REALITY_DEFAULTS["flow"])
    servername = prompt("Servername / SNI", REALITY_DEFAULTS["servername"])
    fingerprint = prompt("client-fingerprint", REALITY_DEFAULTS["client-fingerprint"])
    short_id = prompt("short-id (blank if none)")

    try:
        port_val: int | str = int(port)
    except ValueError:
        port_val = port

    proxy = {
        "name": name,
        "type": "vless",
        "server": server,
        "port": port_val,
        "uuid": uuid,
        "network": network,
        "udp": True,
        "tls": True,
        "client-fingerprint": fingerprint,
        "servername": servername,
        "reality-opts": {"public-key": public_key},
    }
    if flow:
        proxy["flow"] = flow
    if short_id:
        proxy["reality-opts"]["short-id"] = short_id
    return proxy


def add_proxy_raw_yaml() -> dict | None:
    print("\n-- Paste a raw proxy YAML entry --")
    print("Enter a single YAML mapping (the same shape as one item under `proxies:`).")
    print("Finish input with an empty line.")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    raw = "\n".join(lines).strip()
    if not raw:
        print("Nothing entered. Cancelled.")
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"Invalid YAML: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("name") or not data.get("type"):
        print("Entry must be a mapping with at least `name` and `type`. Cancelled.")
        return None
    return data


def add_proxy(proxies: list[dict]) -> bool:
    print("\n1) Guided VLESS + REALITY wizard (recommended for xray-reality)")
    print("2) Paste raw YAML (any proxy type)")
    choice = prompt("Choose", "1")
    proxy = add_proxy_raw_yaml() if choice == "2" else add_proxy_reality_wizard()
    if proxy is None:
        return False
    proxies.append(proxy)
    print(f"Added proxy '{proxy['name']}' (not yet saved to server).")
    return True


def remove_proxy(proxies: list[dict]) -> bool:
    show_proxies(proxies)
    if not proxies:
        return False
    idx = prompt("Index to remove (blank to cancel)")
    if not idx.isdigit() or not (1 <= int(idx) <= len(proxies)):
        print("Cancelled.")
        return False
    removed = proxies.pop(int(idx) - 1)
    print(f"Removed: {removed.get('name')}")
    return True


MENU = """
== SubConfig server secrets ==
 1) Show subscribe URLs
 2) Add subscribe URL
 3) Remove subscribe URL
 4) Show self-hosted proxies
 5) Add self-hosted proxy
 6) Remove self-hosted proxy
 7) Save changes to server
 8) Save & quit
 9) Quit without saving
"""


def main() -> None:
    print("This tool edits secrets stored on your own server instead of GitHub Secrets.")
    print("It assumes `ssh`/`scp` can already log into that server without a password.\n")

    host = prompt("Server host (same as secrets.HOST)")
    if not host:
        print("Host is required.")
        sys.exit(1)
    port = prompt("SSH port (same as secrets.PORT)", DEFAULT_PORT)
    user = prompt("SSH user", DEFAULT_USER)
    remote_dir = prompt("Remote secrets directory", DEFAULT_REMOTE_DIR)

    remote = Remote(host=host, port=port, user=user, remote_dir=remote_dir)

    print(f"\nConnecting to {user}@{host}:{port} ...")
    try:
        urls = load_subscribe(remote.read(SUBSCRIBE_FILE))
        proxies = load_proxies(remote.read(PROXIES_FILE))
    except Exception as exc:
        print(f"Failed to load current config from server: {exc}")
        sys.exit(1)

    print(f"Loaded {len(urls)} subscribe URL(s) and {len(proxies)} self-hosted proxy(ies).")

    def save() -> None:
        remote.write(SUBSCRIBE_FILE, dump_subscribe(urls))
        remote.write(PROXIES_FILE, dump_proxies(proxies))
        print("Saved to server.")

    dirty = False
    while True:
        print(MENU)
        choice = prompt("Choose an option", "1")

        if choice == "1":
            show_subscribe(urls)
        elif choice == "2":
            dirty = add_subscribe(urls) or dirty
        elif choice == "3":
            dirty = remove_subscribe(urls) or dirty
        elif choice == "4":
            show_proxies(proxies)
        elif choice == "5":
            dirty = add_proxy(proxies) or dirty
        elif choice == "6":
            dirty = remove_proxy(proxies) or dirty
        elif choice == "7":
            try:
                save()
                dirty = False
            except subprocess.CalledProcessError as exc:
                print(f"Failed to save: {exc}")
        elif choice == "8":
            if dirty:
                try:
                    save()
                except subprocess.CalledProcessError as exc:
                    print(f"Failed to save: {exc}")
                    if prompt("Quit anyway? (y/n)", "n").lower().startswith("y"):
                        break
                    continue
            break
        elif choice == "9":
            if dirty and not prompt("You have unsaved changes. Quit without saving? (y/n)", "n").lower().startswith("y"):
                continue
            break
        else:
            print("Unknown option.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
