import gzip
import io
import json
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import urlopen

import netbox_nixos

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.json"


def modules(directory: Path) -> Path:
    (directory / "outputs.nix").write_text("{ data, ... }: data\n")
    (directory / "sub").mkdir()
    (directory / "sub" / "default.nix").write_text("{ }\n")
    (directory / "notes.md").write_text("not shipped\n")
    return directory


class FlakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = cast(netbox_nixos.Snapshot, json.loads(FIXTURE.read_text()))
        self.tmp = tempfile.TemporaryDirectory()
        self.modules = modules(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_last_modified_is_the_newest_timestamp(self) -> None:
        # 2026-09-04t08:00:00z, the vm's last_updated in the fixture
        self.assertEqual(netbox_nixos.last_modified(self.snapshot), 1788508800)

    def test_tarball_layout(self) -> None:
        flake = netbox_nixos.build(self.snapshot, self.modules, None)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(flake.tarball))) as tar:
            members = tar.getmembers()
        self.assertEqual(
            [m.name for m in members],
            [
                "netbox-nixos",
                "netbox-nixos/data.json",
                "netbox-nixos/flake.lock",
                "netbox-nixos/flake.nix",
                "netbox-nixos/modules",
                "netbox-nixos/modules/outputs.nix",
                "netbox-nixos/modules/sub",
                "netbox-nixos/modules/sub/default.nix",
            ],
        )
        self.assertEqual(
            [m.name for m in members if m.isdir()],
            ["netbox-nixos", "netbox-nixos/modules", "netbox-nixos/modules/sub"],
        )
        self.assertTrue(all(m.mtime == flake.last_modified for m in members))
        self.assertTrue(all(m.uid == m.gid == 0 for m in members))
        self.assertEqual({m.mode for m in members if m.isdir()}, {0o755})
        self.assertEqual({m.mode for m in members if m.isfile()}, {0o644})

    def test_same_input_gives_the_same_bytes(self) -> None:
        first = netbox_nixos.build(self.snapshot, self.modules, None)
        second = netbox_nixos.build(self.snapshot, self.modules, None)
        self.assertEqual(first, second)

    def test_modules_change_the_bytes(self) -> None:
        before = netbox_nixos.build(self.snapshot, self.modules, None).tarball
        (self.modules / "outputs.nix").write_text("{ data, ... }: { }\n")
        self.assertNotEqual(netbox_nixos.build(self.snapshot, self.modules, None).tarball, before)

    def test_data_json_is_canonical(self) -> None:
        flake = netbox_nixos.build(self.snapshot, self.modules, None)
        self.assertEqual(json.loads(flake.data), self.snapshot)
        self.assertEqual(
            flake.data, json.dumps(self.snapshot, indent=2, sort_keys=True).encode() + b"\n"
        )

    def test_nixpkgs_input_is_declared_only_when_given(self) -> None:
        without = netbox_nixos.flake_nix(None)
        self.assertNotIn("nixpkgs", without)
        self.assertIn("{ ... }:", without)
        with_input = netbox_nixos.flake_nix("github:NixOS/nixpkgs/nixos-25.11")
        self.assertIn('inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";', with_input)
        self.assertIn("{ nixpkgs, ... }:", with_input)
        self.assertIn("inherit nixpkgs;", with_input)
        unlocked = netbox_nixos.build(self.snapshot, self.modules, "github:NixOS/nixpkgs")
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(unlocked.tarball))) as tar:
            self.assertNotIn("netbox-nixos/flake.lock", tar.getnames())


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.modules = modules(Path(self.tmp.name))
        config = netbox_nixos.Config(ttl=60, modules=self.modules, nixpkgs=None)
        self.app = netbox_nixos.App(
            ("127.0.0.1", 0),
            config,
            lambda: cast(netbox_nixos.Snapshot, json.loads(FIXTURE.read_text())),
        )
        self.base = f"http://127.0.0.1:{self.app.server_address[1]}"
        threading.Thread(target=self.app.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.app.shutdown()
        self.app.server_close()
        self.tmp.cleanup()

    def test_serves_the_same_bytes_a_fresh_build_gives(self) -> None:
        snapshot = cast(netbox_nixos.Snapshot, json.loads(FIXTURE.read_text()))
        expected = netbox_nixos.build(snapshot, self.modules, None)
        with urlopen(f"{self.base}/flake.tar.gz") as response:
            self.assertEqual(response.read(), expected.tarball)
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        with urlopen(f"{self.base}/data.json") as response:
            self.assertEqual(response.read(), expected.data)
        with urlopen(f"{self.base}/healthz") as response:
            self.assertEqual(response.read(), b"ok\n")
        with self.assertRaises(HTTPError) as unknown:
            urlopen(f"{self.base}/rev/0/flake.tar.gz")
        self.assertEqual(unknown.exception.code, 404)
        unknown.exception.close()


class NetBoxTest(unittest.TestCase):
    def test_token_version_decides_the_authorization_header(self) -> None:
        self.assertEqual(
            netbox_nixos.NetBox("http://x/", "nbt_key.secret").authorization,
            "Bearer nbt_key.secret",
        )
        self.assertEqual(netbox_nixos.NetBox("http://x/", "legacy\n").authorization, "Token legacy")


if __name__ == "__main__":
    unittest.main()
