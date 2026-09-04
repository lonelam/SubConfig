#!/usr/bin/env python3
"""Interactive tool to manage SubConfig's server-hosted secrets.

Subscribe URLs and self-hosted proxy definitions (e.g. your own
xray-Reality node) are no longer stored as GitHub Secrets. Instead they
live in two files on your own server, at the same host used for
deployment (secrets.HOST in the GitHub Actions workflow):

    <remote-dir>/subscribe.txt              one URL or alias|URL per line
    <remote-dir>/self-hosted-proxies.yaml   YAML list of proxy entries

For Mihomo proxy providers, an optional alias can be placed before the
subscription URL with a ``|`` separator. Existing URL-only files remain
valid, for example:

    Airport A|https://example.com/subscribe?token=...
    机场B|https://example.net/subscribe?token=...
    https://legacy.example.org/subscribe?token=...

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

import hashlib
import re
import shlex
import subprocess
import sys
import unicodedata
from dataclasses import dataclass

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

ALIAS_PATTERN = re.compile(r"^[\w][\w .-]*\w$|^[\w]$", re.UNICODE)
URL_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
MAX_ALIAS_LENGTH = 64
MAX_PROVIDER_NAME_BYTES = 200
RESERVED_PROVIDER_NAMES = {
    "default", "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

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
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to read {remote_path}: {stderr}")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{remote_path} is not valid UTF-8") from exc

    def write(self, filename: str, content: str) -> None:
        remote_path = f"{self.remote_dir}/{filename}"
        mkdir_cmd = self._ssh_base() + [f"mkdir -p {shlex.quote(self.remote_dir)}"]
        subprocess.run(mkdir_cmd, check=True)
        write_cmd = self._ssh_base() + [
            f"cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}"
        ]
        subprocess.run(write_cmd, input=content.encode("utf-8"), check=True)


@dataclass(frozen=True)
class SubscribeEntry:
    url: str
    alias: str | None = None
    is_comment: bool = False


def normalize_alias(raw_alias: str) -> str:
    alias = unicodedata.normalize("NFKC", raw_alias.strip())
    if not alias:
        raise ValueError("alias must not be empty")
    if len(alias) > MAX_ALIAS_LENGTH:
        raise ValueError(f"alias is too long (maximum {MAX_ALIAS_LENGTH} characters)")
    if not ALIAS_PATTERN.fullmatch(alias):
        raise ValueError(
            "alias may only contain Unicode letters/digits, spaces, '.', '-' or '_', "
            "and must start/end with a letter, digit or '_'"
        )
    return alias


def provider_slug(alias: str) -> str:
    slug = re.sub(r"[\s.]+", "-", alias, flags=re.UNICODE)
    slug = re.sub(r"[^\w-]", "-", slug, flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        raise ValueError("alias has no usable provider name")
    if slug.casefold() in RESERVED_PROVIDER_NAMES:
        slug = f"{slug}-provider"
    encoded_slug = slug.encode("utf-8")
    if len(encoded_slug) > MAX_PROVIDER_NAME_BYTES:
        digest = hashlib.sha256(encoded_slug).hexdigest()[:12]
        suffix = f"-{digest}"
        byte_budget = MAX_PROVIDER_NAME_BYTES - len(suffix)
        while len(slug.encode("utf-8")) > byte_budget:
            slug = slug[:-1]
        slug = f"{slug}{suffix}"
    return slug


def validate_mihomo_provider_names(entries: list[SubscribeEntry]) -> None:
    for entry in entries:
        if entry.alias is not None and not re.match(r"^https?://", entry.url, re.I):
            raise ValueError("provider aliases require an http(s) subscription URL")
    http_entries = [entry for entry in entries if re.match(r"^https?://", entry.url, re.I)]
    seen: dict[str, tuple[str, int]] = {}
    total = len(http_entries)
    for index, entry in enumerate(http_entries, start=1):
        if entry.alias is not None:
            name = provider_slug(entry.alias)
        else:
            name = "airport" if total == 1 else f"airport-{index}"
        collision_key = unicodedata.normalize("NFKC", name).casefold()
        if collision_key in seen:
            previous_name, previous_index = seen[collision_key]
            raise ValueError(
                f"Mihomo provider name {name!r} for HTTP entry {index} conflicts with "
                f"{previous_name!r} for HTTP entry {previous_index}"
            )
        seen[collision_key] = (name, index)


def load_subscribe(raw: str) -> list[SubscribeEntry]:
    entries: list[SubscribeEntry] = []
    aliases: dict[str, tuple[str, int]] = {}

    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.lstrip("\ufeff").strip()
        if not line:
            continue
        if line.startswith("#"):
            # Preserve existing comments when the file is loaded and saved.
            entries.append(SubscribeEntry(url=line, is_comment=True))
            continue

        alias: str | None = None
        url = line
        # A legacy URL is recognized before looking for the delimiter, so a
        # literal `|` inside that URL is not mistaken for an alias separator.
        if not URL_SCHEME_PATTERN.match(line) and "|" in line:
            raw_alias, url = line.split("|", 1)
            try:
                alias = normalize_alias(raw_alias)
                alias_slug = provider_slug(alias)
            except ValueError as exc:
                raise ValueError(f"subscribe.txt line {line_no}: {exc}") from exc
            url = url.strip()
            if not url:
                raise ValueError(f"subscribe.txt line {line_no}: URL must not be empty")
            if not re.match(r"^https?://", url, re.I):
                raise ValueError(
                    f"subscribe.txt line {line_no}: aliases require an http(s) subscription URL"
                )

            collision_key = unicodedata.normalize("NFKC", alias_slug).casefold()
            if collision_key in aliases:
                previous_alias, previous_line = aliases[collision_key]
                raise ValueError(
                    f"subscribe.txt line {line_no}: alias {alias!r} conflicts with "
                    f"{previous_alias!r} on line {previous_line}"
                )
            aliases[collision_key] = (alias, line_no)

        entries.append(SubscribeEntry(url=url, alias=alias))

    validate_mihomo_provider_names(entries)
    return entries


def format_subscribe_entry(entry: SubscribeEntry) -> str:
    if entry.is_comment:
        return entry.url
    if entry.alias:
        return f"{entry.alias}|{entry.url}"
    return entry.url


def dump_subscribe(entries: list[SubscribeEntry]) -> str:
    validate_mihomo_provider_names(entries)
    return "".join(f"{format_subscribe_entry(entry)}\n" for entry in entries)


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


def show_subscribe(entries: list[SubscribeEntry]) -> None:
    if not entries:
        print("  (no subscribe URLs configured)")
        return
    for i, entry in enumerate(entries, 1):
        if entry.is_comment:
            print(f"  {i}. # (comment hidden)")
            continue
        alias = f"{entry.alias} | " if entry.alias else ""
        print(f"  {i}. {alias}{mask_middle(entry.url)}")


def show_proxies(proxies: list[dict]) -> None:
    if not proxies:
        print("  (no self-hosted proxies configured)")
        return
    for i, p in enumerate(proxies, 1):
        print(f"  {i}. {p.get('name')}  ({p.get('type')} @ {mask_middle(str(p.get('server', '')))})")


def find_alias_conflict(
    alias: str,
    entries: list[SubscribeEntry],
    ignore_index: int | None = None,
) -> str | None:
    new_key = unicodedata.normalize("NFKC", provider_slug(alias)).casefold()
    for index, entry in enumerate(entries):
        if index == ignore_index or entry.alias is None:
            continue
        existing_slug = provider_slug(entry.alias)
        existing_key = unicodedata.normalize("NFKC", existing_slug).casefold()
        if existing_key == new_key:
            return entry.alias
    return None


def add_subscribe(entries: list[SubscribeEntry]) -> bool:
    raw_alias = prompt("Mihomo provider alias (optional, do not include brackets)")
    alias: str | None = None
    if raw_alias:
        try:
            alias = normalize_alias(raw_alias)
            provider_slug(alias)
        except ValueError as exc:
            print(f"Invalid alias: {exc}. Not added.")
            return False

        conflict = find_alias_conflict(alias, entries)
        if conflict is not None:
            print(f"Alias conflicts with existing provider '{conflict}'. Not added.")
            return False

    url = prompt("Subscribe URL to add (http(s):// or tg://)")
    if not url:
        print("Cancelled.")
        return False
    if not re.match(r"^(?:https?|tg)://", url, re.I):
        print("URL must start with http://, https:// or tg://. Not added.")
        return False
    if alias is not None and not re.match(r"^https?://", url, re.I):
        print("Provider aliases require an http(s) subscription URL. Not added.")
        return False
    new_entry = SubscribeEntry(url=url, alias=alias)
    try:
        validate_mihomo_provider_names([*entries, new_entry])
    except ValueError as exc:
        print(f"Provider name conflict: {exc}. Not added.")
        return False
    entries.append(new_entry)
    print("Added (not yet saved to server).")
    return True


def update_subscribe_alias(entries: list[SubscribeEntry]) -> bool:
    show_subscribe(entries)
    if not entries:
        return False
    idx = prompt("Index to update (blank to cancel)")
    if not idx.isdigit() or not (1 <= int(idx) <= len(entries)):
        print("Cancelled.")
        return False

    index = int(idx) - 1
    current = entries[index]
    if current.is_comment:
        print("Comments cannot have provider aliases.")
        return False
    if not re.match(r"^https?://", current.url, re.I):
        print("Only http(s) subscriptions can have Mihomo provider aliases.")
        return False
    current_alias = current.alias or "(none)"
    print(f"Current alias: {current_alias}")
    raw_alias = prompt("New alias ('-' to remove, blank to cancel)")
    if not raw_alias:
        print("Cancelled.")
        return False

    if raw_alias == "-":
        alias = None
    else:
        try:
            alias = normalize_alias(raw_alias)
            provider_slug(alias)
        except ValueError as exc:
            print(f"Invalid alias: {exc}. Not changed.")
            return False
        conflict = find_alias_conflict(alias, entries, ignore_index=index)
        if conflict is not None:
            print(f"Alias conflicts with existing provider '{conflict}'. Not changed.")
            return False

    if alias == current.alias:
        print("Alias is unchanged.")
        return False
    updated = SubscribeEntry(url=current.url, alias=alias)
    candidate_entries = [*entries]
    candidate_entries[index] = updated
    try:
        validate_mihomo_provider_names(candidate_entries)
    except ValueError as exc:
        print(f"Provider name conflict: {exc}. Not changed.")
        return False
    entries[index] = updated
    print(f"Alias updated to: {alias or '(none)'} (not yet saved to server).")
    return True


def remove_subscribe(entries: list[SubscribeEntry]) -> bool:
    show_subscribe(entries)
    if not entries:
        return False
    idx = prompt("Index to remove (blank to cancel)")
    if not idx.isdigit() or not (1 <= int(idx) <= len(entries)):
        print("Cancelled.")
        return False
    index = int(idx) - 1
    candidate_entries = entries[:index] + entries[index + 1:]
    try:
        validate_mihomo_provider_names(candidate_entries)
    except ValueError as exc:
        print(f"Removing this entry would cause a provider name conflict: {exc}. Not removed.")
        return False
    removed = entries.pop(index)
    if removed.is_comment:
        print("Removed comment.")
        return True
    alias = f"{removed.alias} | " if removed.alias else ""
    print(f"Removed: {alias}{mask_middle(removed.url)}")
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
 2) Add subscribe URL / provider alias
 3) Set/change/remove provider alias
 4) Remove subscribe URL
 5) Show self-hosted proxies
 6) Add self-hosted proxy
 7) Remove self-hosted proxy
 8) Save changes to server
 9) Save & quit
10) Quit without saving
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
        subscriptions = load_subscribe(remote.read(SUBSCRIBE_FILE))
        proxies = load_proxies(remote.read(PROXIES_FILE))
    except Exception as exc:
        print(f"Failed to load current config from server: {exc}")
        sys.exit(1)

    subscription_count = sum(not entry.is_comment for entry in subscriptions)
    print(f"Loaded {subscription_count} subscribe URL(s) and {len(proxies)} self-hosted proxy(ies).")

    def save() -> None:
        remote.write(SUBSCRIBE_FILE, dump_subscribe(subscriptions))
        remote.write(PROXIES_FILE, dump_proxies(proxies))
        print("Saved to server.")

    dirty = False
    while True:
        print(MENU)
        choice = prompt("Choose an option", "1")

        if choice == "1":
            show_subscribe(subscriptions)
        elif choice == "2":
            dirty = add_subscribe(subscriptions) or dirty
        elif choice == "3":
            dirty = update_subscribe_alias(subscriptions) or dirty
        elif choice == "4":
            dirty = remove_subscribe(subscriptions) or dirty
        elif choice == "5":
            show_proxies(proxies)
        elif choice == "6":
            dirty = add_proxy(proxies) or dirty
        elif choice == "7":
            dirty = remove_proxy(proxies) or dirty
        elif choice == "8":
            try:
                save()
                dirty = False
            except (subprocess.CalledProcessError, ValueError) as exc:
                print(f"Failed to save: {exc}")
        elif choice == "9":
            if dirty:
                try:
                    save()
                except (subprocess.CalledProcessError, ValueError) as exc:
                    print(f"Failed to save: {exc}")
                    if prompt("Quit anyway? (y/n)", "n").lower().startswith("y"):
                        break
                    continue
            break
        elif choice == "10":
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
