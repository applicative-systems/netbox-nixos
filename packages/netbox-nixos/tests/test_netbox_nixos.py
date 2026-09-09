import gzip
import io
import json
import tarfile
import tempfile
import threading
import unittest
from collections.abc import Callable
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import urlopen

import netbox_nixos

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.json"


def members(tarball: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tarball))) as tar:
        return tar.getmembers()


class TarballTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = cast(netbox_nixos.Snapshot, json.loads(FIXTURE.read_text()))
        self.tmp = tempfile.TemporaryDirectory()
        self.modules = Path(self.tmp.name)
        (self.modules / "outputs.nix").write_text("{ data, ... }: data\n")
        (self.modules / "sub").mkdir()
        (self.modules / "sub" / "default.nix").write_text("{ }\n")
        (self.modules / "notes.md").write_text("not shipped\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_layout(self) -> None:
        entries = members(netbox_nixos.tarball(self.snapshot, self.modules, None))
        self.assertEqual(
            [m.name for m in entries],
            [
                "netbox-nixos/data.json",
                "netbox-nixos/flake.lock",
                "netbox-nixos/flake.nix",
                "netbox-nixos/modules/outputs.nix",
                "netbox-nixos/modules/sub/default.nix",
            ],
        )
        self.assertTrue(
            all(m.mtime == 0 and m.uid == m.gid == 0 and m.mode == 0o644 for m in entries)
        )

    def test_same_input_gives_the_same_bytes(self) -> None:
        self.assertEqual(
            netbox_nixos.tarball(self.snapshot, self.modules, None),
            netbox_nixos.tarball(self.snapshot, self.modules, None),
        )

    def test_modules_change_the_bytes(self) -> None:
        before = netbox_nixos.tarball(self.snapshot, self.modules, None)
        (self.modules / "outputs.nix").write_text("{ data, ... }: { }\n")
        self.assertNotEqual(netbox_nixos.tarball(self.snapshot, self.modules, None), before)

    def test_data_json_is_canonical(self) -> None:
        tarball = netbox_nixos.tarball(self.snapshot, self.modules, None)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tarball))) as tar:
            data = tar.extractfile("netbox-nixos/data.json")
            assert data is not None
            self.assertEqual(
                data.read(), json.dumps(self.snapshot, indent=2, sort_keys=True).encode() + b"\n"
            )

    def test_nixpkgs_input_is_declared_only_when_given(self) -> None:
        without = netbox_nixos.flake_nix(None)
        self.assertNotIn("nixpkgs", without)
        self.assertIn("{ ... }:", without)
        with_input = netbox_nixos.flake_nix("github:NixOS/nixpkgs/nixos-25.11")
        self.assertIn('inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";', with_input)
        self.assertIn("{ nixpkgs, ... }:", with_input)
        self.assertIn("inherit nixpkgs;", with_input)
        unlocked = netbox_nixos.tarball(self.snapshot, self.modules, "github:NixOS/nixpkgs")
        self.assertNotIn("netbox-nixos/flake.lock", [m.name for m in members(unlocked)])


class ServerTest(unittest.TestCase):
    def serve(self, flake: Callable[[], bytes]) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(netbox_nixos.Handler, flake))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_serves_the_flake(self) -> None:
        base = self.serve(lambda: b"bytes")
        with urlopen(f"{base}/flake.tar.gz") as response:
            self.assertEqual(response.read(), b"bytes")
            self.assertEqual(response.headers["Content-Type"], "application/gzip")
        with self.assertRaises(HTTPError) as unknown:
            urlopen(f"{base}/data.json")
        self.assertEqual(unknown.exception.code, 404)
        unknown.exception.close()

    def test_an_export_failure_is_a_503(self) -> None:
        def failing() -> bytes:
            raise RuntimeError("netbox is down")

        base = self.serve(failing)
        with self.assertRaises(HTTPError) as failure:
            urlopen(f"{base}/flake.tar.gz")
        self.assertEqual(failure.exception.code, 503)
        self.assertIn(b"netbox is down", failure.exception.read())


if __name__ == "__main__":
    unittest.main()
