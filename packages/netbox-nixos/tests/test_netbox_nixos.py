import gzip
import io
import json
import re
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


class FlakeTest(unittest.TestCase):
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

    def test_last_modified_is_the_newest_timestamp(self) -> None:
        # 2026-09-04t08:00:00z, the vm's last_updated in the fixture
        self.assertEqual(netbox_nixos.last_modified(self.snapshot), 1788508800)

    def test_tarball_layout(self) -> None:
        flake = netbox_nixos.build(self.snapshot, self.modules, None)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(flake.tarball))) as tar:
            members = tar.getmembers()
        top = f"netbox-nixos-{flake.rev}"
        self.assertEqual(
            [m.name for m in members],
            [
                top,
                f"{top}/data.json",
                f"{top}/flake.lock",
                f"{top}/flake.nix",
                f"{top}/modules",
                f"{top}/modules/outputs.nix",
                f"{top}/modules/sub",
                f"{top}/modules/sub/default.nix",
            ],
        )
        self.assertEqual(
            [m.name for m in members if m.isdir()], [top, f"{top}/modules", f"{top}/modules/sub"]
        )
        self.assertTrue(all(m.mtime == flake.last_modified for m in members))
        self.assertTrue(all(m.uid == m.gid == 0 for m in members))
        self.assertEqual({m.mode for m in members if m.isdir()}, {0o755})
        self.assertEqual({m.mode for m in members if m.isfile()}, {0o644})

    def test_same_input_gives_the_same_bytes(self) -> None:
        first = netbox_nixos.build(self.snapshot, self.modules, None)
        second = netbox_nixos.build(self.snapshot, self.modules, None)
        self.assertEqual(first, second)
        self.assertRegex(first.rev, r"^[0-9a-f]{40}$")

    def test_modules_are_part_of_the_revision(self) -> None:
        before = netbox_nixos.build(self.snapshot, self.modules, None).rev
        (self.modules / "outputs.nix").write_text("{ data, ... }: { }\n")
        self.assertNotEqual(netbox_nixos.build(self.snapshot, self.modules, None).rev, before)

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
        locked = netbox_nixos.build(self.snapshot, self.modules, "github:NixOS/nixpkgs")
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(locked.tarball))) as tar:
            self.assertNotIn(f"netbox-nixos-{locked.rev}/flake.lock", tar.getnames())


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        modules = Path(self.tmp.name)
        (modules / "outputs.nix").write_text("{ data, ... }: data\n")
        config = netbox_nixos.Config(
            public_url="http://example.org/nix", ttl=60, modules=modules, nixpkgs=None
        )
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

    def test_link_header_names_an_immutable_url_with_the_same_bytes(self) -> None:
        with urlopen(f"{self.base}/flake.tar.gz") as response:
            tarball = response.read()
            link = response.headers["Link"]
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        m = re.fullmatch(
            r"<http://example\.org/nix/rev/([0-9a-f]{40})/flake\.tar\.gz"
            r'\?rev=\1&lastModified=(\d+)>; rel="immutable"',
            link,
        )
        self.assertIsNotNone(m, link)
        assert m is not None
        with urlopen(f"{self.base}/rev/{m[1]}/flake.tar.gz") as response:
            self.assertEqual(response.read(), tarball)
            self.assertIn("immutable", response.headers["Cache-Control"])
        with urlopen(f"{self.base}/rev/{m[1]}/data.json") as response:
            self.assertEqual(json.load(response)["netbox_version"], "4.6.8")
        with self.assertRaises(HTTPError) as unknown:
            urlopen(f"{self.base}/rev/{'0' * 40}/flake.tar.gz")
        self.assertEqual(unknown.exception.code, 404)
        unknown.exception.close()
        with urlopen(f"{self.base}/healthz") as response:
            self.assertEqual(response.read(), b"ok\n")


class NetBoxTest(unittest.TestCase):
    def test_token_version_decides_the_authorization_header(self) -> None:
        self.assertEqual(
            netbox_nixos.NetBox("http://x/", "nbt_key.secret").authorization,
            "Bearer nbt_key.secret",
        )
        self.assertEqual(netbox_nixos.NetBox("http://x/", "legacy\n").authorization, "Token legacy")


if __name__ == "__main__":
    unittest.main()
