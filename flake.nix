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

    apps = builtins.mapAttrs (_system: pkgs: {
      demo = {
        type = "app";
        program = "${
          inputs.self.checks.${system}.integration.driverInteractive
        }/bin/nixos-test-driver";
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
            ruff format --check .
            mypy netbox_nixos.py tests/test_netbox_nixos.py
            touch $out
          '';
      format = pkgs.runCommand "netbox-nixos-format" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
        nixfmt --check \
          ${inputs.self}/flake.nix \
          ${inputs.self}/modules \
          ${inputs.self}/tests \
          ${inputs.self}/packages/netbox-nixos/package.nix
        touch $out
      '';
    }) inputs.nixpkgs.legacyPackages;

    devShells = builtins.mapAttrs (_system: pkgs: {
      default = pkgs.mkShell {
        packages = with pkgs; [
          python3
          ruff
          mypy
          nixfmt
          jq
        ];
      };
    }) inputs.nixpkgs.legacyPackages;

    formatter = builtins.mapAttrs (_system: pkgs: pkgs.nixfmt-tree) inputs.nixpkgs.legacyPackages;
  };
}
