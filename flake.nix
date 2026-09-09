{
  description = "serve netbox data as a nix flake";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        netbox-nixos = pkgs.callPackage ./packages/netbox-nixos/package.nix {
          modules = ./modules/netbox;
        };
        default = netbox-nixos;
      });

      lib.outputs = import ./modules/netbox/outputs.nix;

      nixosModules.server =
        { lib, pkgs, ... }:
        {
          imports = [ ./modules/netbox-nixos.nix ];
          services.netbox-nixos.package =
            lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.netbox-nixos;
        };

      apps = forAll (pkgs: {
        demo = {
          type = "app";
          program = "${
            self.checks.${pkgs.stdenv.hostPlatform.system}.integration.driverInteractive
          }/bin/nixos-test-driver";
          meta.description = "the vm test's netbox and server, reachable from the host";
        };
      });

      checks = forAll (pkgs: {
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.netbox-nixos;
        eval = import ./tests/eval.nix {
          inherit pkgs;
          inherit (pkgs) lib;
        };
        integration = import ./tests/integration.nix { inherit pkgs self; };
        lint =
          pkgs.runCommand "netbox-nixos-lint"
            {
              nativeBuildInputs = with pkgs; [
                mypy
                python3
                ruff
              ];
            }
            ''
              cd ${self}/packages/netbox-nixos
              export RUFF_CACHE_DIR=$TMPDIR/ruff MYPY_CACHE_DIR=$TMPDIR/mypy
              ruff check .
              ruff format --check .
              mypy netbox_nixos.py tests/test_netbox_nixos.py
              touch $out
            '';
        format = pkgs.runCommand "netbox-nixos-format" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
          nixfmt --check ${self}/flake.nix ${self}/modules ${self}/tests ${self}/packages/netbox-nixos/package.nix
          touch $out
        '';
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            ruff
            mypy
            nixfmt
            jq
          ];
        };
      });

      formatter = forAll (pkgs: pkgs.nixfmt);
    };
}
