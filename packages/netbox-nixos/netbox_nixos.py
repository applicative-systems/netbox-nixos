"""serve netbox as a nix flake.

nix locks a tarball url by the nar hash of what it fetched and the newest
mtime inside, so identical data must give identical bytes.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import tarfile
from collections.abc import Callable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.request import Request, urlopen

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None
type Snapshot = dict[str, list[Json]]


# field names follow netbox's strawberry types in dcim/graphql/types.py,
# ipam/graphql/types.py and virtualization/graphql/types.py
def query(list_name: str, interface_extra: str) -> str:
    return (
        "query ($platform: String!, $start: Int!, $limit: Int!) { "
        f"{list_name}(filters: {{ platform: {{ slug: {{ exact: $platform }} }} }}, "
        "pagination: { start: $start, limit: $limit }) { "
        "id name status site { slug } role { slug } platform { slug } "
        "primary_ip4 { address } primary_ip6 { address } tags { slug } "
        "custom_fields config_context "
        "services { name protocol ports } "
        f"interfaces {{ name enabled mtu mode description {interface_extra} "
        "primary_mac_address { mac_address } parent { name } "
        "untagged_vlan { vid name } tagged_vlans { vid name } "
        "ip_addresses { address status dns_name } } } }"
    )


def graphql(url: str, token: str, query: str, variables: Json) -> Json:
    # v2 tokens contain a dot and go in a bearer header
    # (netbox/api/authentication.py)
    authorization = f"Bearer {token}" if "." in token else f"Token {token}"
    request = Request(
        f"{url}/graphql/",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": authorization, "Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        reply = cast(dict[str, Json], json.load(response))
    if "errors" in reply:
        raise RuntimeError(str(reply["errors"]))
    return reply["data"]


def hosts(url: str, token: str, platform: str, list_name: str, interface_extra: str) -> list[Json]:
    items: list[Json] = []
    start = 0
    while True:
        variables: Json = {"platform": platform, "start": start, "limit": 500}
        data = graphql(url, token, query(list_name, interface_extra), variables)
        page = cast(list[dict[str, Json]], cast(dict[str, Json], data)[list_name])
        items.extend(page)
        if len(page) < 500:
            return items
        # cursor pagination (netbox >= 4.5.2): `start` is the smallest primary key to return
        start = int(cast(str, page[-1]["id"])) + 1


def snapshot(url: str, token: str, platform: str) -> Snapshot:
    # vm interfaces have neither a type nor mgmt_only
    return {
        "devices": hosts(url, token, platform, "device_list", "type mgmt_only"),
        "virtual_machines": hosts(url, token, platform, "virtual_machine_list", ""),
    }


# nix refuses to evaluate a remote flake whose lock file it would have to
# write, even one without inputs
EMPTY_LOCK = b'{\n  "nodes": {\n    "root": {}\n  },\n  "root": "root",\n  "version": 7\n}\n'


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


def tarball(snapshot: Snapshot, modules: Path, nixpkgs: str | None) -> bytes:
    files = {
        "flake.nix": flake_nix(nixpkgs).encode(),
        # sorted keys: identical data, identical bytes
        "data.json": json.dumps(snapshot, indent=2, sort_keys=True).encode() + b"\n",
        **{f"modules/{p.relative_to(modules)}": p.read_bytes() for p in modules.rglob("*.nix")},
    }
    # with a nixpkgs input, locking is left to the flake that uses this one
    if nixpkgs is None:
        files["flake.lock"] = EMPTY_LOCK
    buffer = io.BytesIO()
    # every mtime stays 0: nix reads `lastModified` off the newest one and
    # compares it with the lock. nix strips the single top-level directory
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for name in sorted(files):
            info = tarfile.TarInfo(f"netbox-nixos/{name}")
            info.size = len(files[name])
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def __init__(self, flake: Callable[[], bytes], *args: Any) -> None:
        self.flake = flake
        super().__init__(*args)

    def do_GET(self) -> None:
        if self.path != "/flake.tar.gz":
            self.send_error(404)
            return
        try:
            body = self.flake()
        except Exception as e:
            self.send_error(503, explain=str(e))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="serve netbox as a nix flake")
    parser.add_argument("--netbox-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--listen", default="127.0.0.1:8080")
    parser.add_argument("--platform", default="nixos", help="platform slug of the nixos hosts")
    parser.add_argument(
        "--nixpkgs", help="flakeref declared as the nixpkgs input of the served flake"
    )
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    modules = Path(os.environ.get("NETBOX_NIXOS_MODULES", "modules/netbox"))

    def flake() -> bytes:
        return tarball(snapshot(args.netbox_url, token, args.platform), modules, args.nixpkgs)

    host, port = args.listen.rsplit(":", 1)
    ThreadingHTTPServer((host.strip("[]"), int(port)), partial(Handler, flake)).serve_forever()


if __name__ == "__main__":
    main()
