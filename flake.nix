{
  description = "serve netbox data as a nix flake";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = inputs: {
    packages = builtins.mapAttrs (system: pkgs: {
      netbox-nixos = pkgs.callPackage ./packages/netbox-nixos/package.nix {
        modules = ./modules/netbox;
      };
      default = inputs.self.packages.${system}.netbox-nixos;
    }) inputs.nixpkgs.legacyPackages;

    lib.outputs = import ./modules/netbox/outputs.nix;

    nixosModules.server =
      { lib, pkgs, ... }:
      {
        imports = [ ./modules/netbox-nixos.nix ];
        services.netbox-nixos.package =
          lib.mkDefault
            inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.netbox-nixos;
      };

    apps = builtins.mapAttrs (system: _pkgs: {
      demo = {
        type = "app";
        program = "${inputs.self.checks.${system}.integration.driverInteractive}/bin/nixos-test-driver";
        meta.description = "the vm test's netbox and server, reachable from the host";
      };
    }) inputs.nixpkgs.legacyPackages;

    checks = builtins.mapAttrs (system: pkgs: {
      package = inputs.self.packages.${system}.netbox-nixos;
      eval = import ./tests/eval.nix {
        inherit pkgs;
        inherit (pkgs) lib;
      };
      integration = import ./tests/integration.nix {
        inherit pkgs;
        inherit (inputs) self;
      };
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
            cd ${inputs.self}/packages/netbox-nixos
            export RUFF_CACHE_DIR=$TMPDIR/ruff MYPY_CACHE_DIR=$TMPDIR/mypy
            ruff check .
            mypy netbox_nixos.py tests/test_netbox_nixos.py
            touch $out
          '';
      formatting = inputs.self.formatter.${system}.check inputs.self;
    }) inputs.nixpkgs.legacyPackages;

    devShells = builtins.mapAttrs (system: pkgs: {
      default = pkgs.mkShell {
        packages = with pkgs; [
          python3
          ruff
          mypy
          jq
          inputs.self.formatter.${system}
        ];
      };
    }) inputs.nixpkgs.legacyPackages;

    formatter = builtins.mapAttrs (
      _system: pkgs:
      pkgs.treefmt.withConfig {
        settings = {
          tree-root-file = "flake.nix";
          on-unmatched = "info";
          formatter = {
            nixfmt = {
              command = pkgs.lib.getExe pkgs.nixfmt;
              includes = [ "*.nix" ];
            };
            statix = {
              command = pkgs.lib.getExe pkgs.statix;
              options = [ "fix" ];
              no-positional-arg-support = true;
              includes = [ "*.nix" ];
            };
            deadnix = {
              command = pkgs.lib.getExe pkgs.deadnix;
              options = [ "--edit" ];
              includes = [ "*.nix" ];
            };
            ruff-format = {
              command = pkgs.lib.getExe pkgs.ruff;
              options = [ "format" ];
              includes = [ "*.py" ];
            };
            ruff-check = {
              command = pkgs.lib.getExe pkgs.ruff;
              options = [
                "check"
                "--fix"
              ];
              includes = [ "*.py" ];
            };
            prettier = {
              command = pkgs.lib.getExe pkgs.prettier;
              options = [ "--write" ];
              includes = [ "*.md" ];
            };
          };
        };
      }
    ) inputs.nixpkgs.legacyPackages;
  };
}
