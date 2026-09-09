"""serve netbox as a nix flake.

the mutable url answers with a `link: <url>; rel="immutable"` header, per the
"serving tarball flakes" chapter of the nix manual. nix records that url in
flake.lock together with the nar hash it computes itself, and the url keeps
serving the same bytes for as long as the process lives.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("netbox-nixos")

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


class Snapshot(TypedDict):
    netbox_version: str
    devices: list[Json]
    virtual_machines: list[Json]


# field names follow netbox's strawberry types in dcim/graphql/types.py,
# ipam/graphql/types.py and virtualization/graphql/types.py
IP_ADDRESS = "ip_addresses { address status dns_name last_updated }"
DEVICE_INTERFACE = (
    "interfaces { name type enabled mgmt_only mtu mode description "
    "primary_mac_address { mac_address } parent { name } untagged_vlan { vid name } "
    f"tagged_vlans {{ vid name }} {IP_ADDRESS} }}"
)
# vm interfaces have neither a type nor mgmt_only
VM_INTERFACE = (
    "interfaces { name enabled mtu mode description "
    "primary_mac_address { mac_address } parent { name } untagged_vlan { vid name } "
    f"tagged_vlans {{ vid name }} {IP_ADDRESS} }}"
)
HOST_FIELDS = (
    "id name status last_updated site { slug } role { slug } platform { slug } "
    "primary_ip4 { address } primary_ip6 { address } tags { slug } custom_fields config_context "
    "services { name protocol ports }"
)


def host_query(list_name: str, interface: str) -> str:
    return (
        "query ($platform: String!, $start: Int!, $limit: Int!) { "
        f"{list_name}(filters: {{ platform: {{ slug: {{ exact: $platform }} }} }}, "
        "pagination: { start: $start, limit: $limit }) "
        f"{{ {HOST_FIELDS} {interface} }} }}"
    )


class NetBox:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        # v2 tokens are "<key>.<secret>" (prefixed with "nbt_" since 4.6) and
        # go in a bearer header, v1 tokens never contain a dot
        # (netbox/api/authentication.py)
        token = token.strip()
        self.authorization = f"Bearer {token}" if "." in token else f"Token {token}"

    def snapshot(self, platform: str) -> Snapshot:
        version = self.request("/api/status/")["netbox-version"]
        if not isinstance(version, str):
            raise RuntimeError("/api/status/ has no netbox-version")
        return {
            "netbox_version": version,
            "devices": self.list("device_list", DEVICE_INTERFACE, platform),
            "virtual_machines": self.list("virtual_machine_list", VM_INTERFACE, platform),
        }

    # cursor pagination (netbox >= 4.5.2): `start` is the smallest primary
    # key to return, so the next page begins after the last id seen
    def list(self, list_name: str, interface: str, platform: str) -> list[Json]:
        query = host_query(list_name, interface)
        limit = 500
        start = 0
        items: list[Json] = []
        while True:
            variables: Json = {"platform": platform, "start": start, "limit": limit}
            body = self.request("/graphql/", {"query": query, "variables": variables})
            if "errors" in body:
                raise RuntimeError(f"graphql {list_name}: {body['errors']}")
            data = body["data"]
            page = data.get(list_name) if isinstance(data, dict) else None
            if not isinstance(page, list):
                raise RuntimeError(f"graphql {list_name}: no list in response")
            items.extend(page)
            if len(page) < limit:
                return items
            last = page[-1]
            last_id = last.get("id") if isinstance(last, dict) else None
            if not isinstance(last_id, str):
                raise RuntimeError(f"graphql {list_name}: item without id")
            start = int(last_id) + 1

    def request(self, path: str, body: Json = None) -> dict[str, Json]:
        request = Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": self.authorization,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request) as response:
            reply: Json = json.load(response)
        if not isinstance(reply, dict):
            raise RuntimeError(f"{path}: unexpected response")
        return reply


# nix refuses to evaluate a remote flake whose lock file it would have to
# write, even one without inputs
EMPTY_LOCK = b'{\n  "nodes": {\n    "root": {}\n  },\n  "root": "root",\n  "version": 7\n}\n'


@dataclass(frozen=True)
class Flake:
    rev: str
    last_modified: int
    tarball: bytes
    data: bytes


def build(snapshot: Snapshot, modules: Path, nixpkgs: str | None) -> Flake:
    # sorted keys, so the same data always gives the same bytes
    data = json.dumps(snapshot, indent=2, sort_keys=True).encode() + b"\n"
    files = {"flake.nix": flake_nix(nixpkgs).encode(), "data.json": data}
    # with a nixpkgs input, locking is left to the flake that uses this one
    if nixpkgs is None:
        files["flake.lock"] = EMPTY_LOCK
    for path in sorted(modules.rglob("*.nix")):
        files[f"modules/{path.relative_to(modules)}"] = path.read_bytes()

    # a sha1 so that nix accepts it as `rev`; it covers modules and data
    digest = hashlib.sha1()
    for name, content in sorted(files.items()):
        digest.update(f"{name}\0{len(content)}\0".encode())
        digest.update(content)
    rev = digest.hexdigest()

    newest = last_modified(snapshot)
    return Flake(rev, newest, tar_gz(f"netbox-nixos-{rev}", files, newest), data)


def last_modified(snapshot: Snapshot) -> int:
    """the newest `last_updated` in the export, so that it moves only when netbox data did"""
    return max(newest(snapshot["devices"]), newest(snapshot["virtual_machines"]))


def newest(value: Json) -> int:
    if isinstance(value, dict):
        return max(
            (
                int(datetime.fromisoformat(v).timestamp())
                if k == "last_updated" and isinstance(v, str)
                else newest(v)
                for k, v in value.items()
            ),
            default=0,
        )
    if isinstance(value, list):
        return max(map(newest, value), default=0)
    return 0


def flake_nix(nixpkgs: str | None) -> str:
    # a formal argument of `outputs` that is not declared under `inputs` would
    # be an implicit input resolved through the flake registry
    inputs = f'\n  inputs.nixpkgs.url = "{nixpkgs}";\n' if nixpkgs else ""
    formal = "nixpkgs, " if nixpkgs else ""
    passthrough = "      inherit nixpkgs;\n" if nixpkgs else ""
    return f"""{{
  description = "nixos modules generated from netbox";
{inputs}
  outputs =
    {{ {formal}... }}:
    import ./modules/outputs.nix {{
{passthrough}      data = builtins.fromJSON (builtins.readFile ./data.json);
    }};
}}
"""


def tar_gz(top: str, files: dict[str, bytes], mtime: int) -> bytes:
    """nix strips a single top-level directory, and checks `lastModified`
    against the newest mtime in the archive, so every entry gets `mtime`"""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for name, content in entries(top, files):
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def entries(top: str, files: dict[str, bytes]) -> Iterator[tuple[str, bytes | None]]:
    paths: dict[str, bytes | None] = {top: None}
    for name, content in files.items():
        for parent in Path(name).parents:
            if parent.name:
                paths[f"{top}/{parent}"] = None
        paths[f"{top}/{name}"] = content
    for name in sorted(paths):
        yield (name if paths[name] is not None else name + "/"), paths[name]


@dataclass(frozen=True)
class Config:
    public_url: str
    ttl: float
    modules: Path
    nixpkgs: str | None


class App(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], config: Config, source: Callable[[], Snapshot]
    ) -> None:
        super().__init__(address, Handler)
        self.config = config
        self.source = source
        self.lock = threading.Lock()
        self.current: tuple[Flake, float] | None = None
        # revisions live only as long as the process; see the readme
        self.revisions: dict[str, Flake] = {}

    def latest(self) -> Flake:
        with self.lock:
            if self.current and time.monotonic() - self.current[1] < self.config.ttl:
                return self.current[0]
            try:
                flake = build(self.source(), self.config.modules, self.config.nixpkgs)
            except Exception:
                # a stale flake beats an outage for the hosts pulling from us
                if self.current is None:
                    raise
                log.exception("export failed, serving revision %s", self.current[0].rev)
                return self.current[0]
            if flake.rev not in self.revisions:
                log.info("revision %s", flake.rev)
                self.revisions[flake.rev] = flake
            self.current = (flake, time.monotonic())
            return flake

    def revision(self, rev: str) -> Flake | None:
        with self.lock:
            return self.revisions.get(rev)


class Handler(BaseHTTPRequestHandler):
    server: App

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.reply(200, "text/plain", b"ok\n")
            return
        if path in ("/flake.tar.gz", "/data.json"):
            try:
                flake = self.server.latest()
            except Exception as e:
                log.exception("export failed")
                self.reply(503, "text/plain", f"{e}\n".encode())
                return
            cache_control = "no-cache"
        elif (m := re.fullmatch(r"/rev/([0-9a-f]{40})/(flake\.tar\.gz|data\.json)", path)) and (
            found := self.server.revision(m[1])
        ):
            flake = found
            cache_control = "public, max-age=31536000, immutable"
        else:
            self.reply(404, "text/plain", b"not found\n")
            return
        if path.endswith("data.json"):
            self.reply(200, "application/json", flake.data, cache_control)
            return
        link = None
        if path == "/flake.tar.gz":
            url = self.server.config.public_url
            link = (
                f"<{url}/rev/{flake.rev}/flake.tar.gz?rev={flake.rev}"
                f'&lastModified={flake.last_modified}>; rel="immutable"'
            )
        self.reply(200, "application/gzip", flake.tarball, cache_control, link)

    def reply(
        self,
        status: int,
        content_type: str,
        body: bytes,
        cache_control: str | None = None,
        link: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.info(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(prog="netbox-nixos", description="serve netbox as a nix flake")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve netbox as a flake over http")
    serve.add_argument(
        "--netbox-url", default=os.environ.get("NETBOX_URL"), help="base url of netbox"
    )
    serve.add_argument(
        "--token-file",
        type=Path,
        default=os.environ.get("NETBOX_TOKEN_FILE"),
        help="file containing the api token",
    )
    serve.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"), help=argparse.SUPPRESS)
    serve.add_argument(
        "--fixture", type=Path, help="serve a recorded response instead of talking to netbox"
    )
    serve.add_argument("--listen", default="127.0.0.1:8080", help="address and port to listen on")
    serve.add_argument(
        "--public-url", help="url under which clients reach this server; used in link headers"
    )
    serve.add_argument(
        "--nixpkgs", help="flakeref to declare as the nixpkgs input of the served flake"
    )
    serve.add_argument(
        "--platform",
        default="nixos",
        help="platform slug that marks a device or vm as a nixos host",
    )
    serve.add_argument(
        "--ttl", type=float, default=30, help="seconds before the export is refreshed"
    )
    serve.add_argument(
        "--modules",
        type=Path,
        default=Path(os.environ.get("NETBOX_NIXOS_MODULES", "modules/netbox")),
        help="directory with the nix modules to bundle",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if (args.fixture is None) == (args.netbox_url is None):
        parser.error("exactly one of --netbox-url or --fixture is required")
    source: Callable[[], Snapshot]
    if args.fixture is not None:

        def source() -> Snapshot:
            return cast(Snapshot, json.loads(args.fixture.read_text()))

    else:
        token = args.token_file.read_text() if args.token_file else args.token
        if token is None:
            parser.error("--token-file or --token is required with --netbox-url")
        netbox = NetBox(args.netbox_url, token)

        def source() -> Snapshot:
            return netbox.snapshot(args.platform)

    host, port = args.listen.rsplit(":", 1)
    config = Config(
        public_url=(args.public_url or f"http://{args.listen}").rstrip("/"),
        ttl=args.ttl,
        modules=args.modules,
        nixpkgs=args.nixpkgs,
    )
    app = App((host.strip("[]"), int(port)), config, source)
    log.info("listening on %s", args.listen)
    app.serve_forever()


if __name__ == "__main__":
    main()
