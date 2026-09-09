{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.netbox-nixos;
in
{
  options.services.netbox-nixos = {
    enable = lib.mkEnableOption "serving netbox as a nix flake";
    package = lib.mkPackageOption pkgs "netbox-nixos" { };
    netboxUrl = lib.mkOption {
      type = lib.types.str;
      example = "https://netbox.example.org";
      description = "base url of the netbox instance";
    };
    tokenFile = lib.mkOption {
      type = lib.types.str;
      description = "file containing the netbox api token; passed as a systemd credential, never copied to the store";
    };
    listen = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1:8080";
      description = "address and port to listen on";
    };
    nixpkgs = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "github:NixOS/nixpkgs/nixos-25.11";
      description = "flakeref declared as the nixpkgs input of the served flake";
    };
    platform = lib.mkOption {
      type = lib.types.str;
      default = "nixos";
      description = "platform slug that marks a device or virtual machine as a nixos host";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.netbox-nixos = {
      description = "netbox as a nix flake";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      serviceConfig = {
        ExecStart =
          "${lib.getExe cfg.package} "
          + lib.cli.toCommandLineShellGNU { } {
            netbox-url = cfg.netboxUrl;
            token-file = "%d/token";
            listen = cfg.listen;
            nixpkgs = cfg.nixpkgs;
            platform = cfg.platform;
          };
        LoadCredential = [ "token:${cfg.tokenFile}" ];
        DynamicUser = true;
        Restart = "on-failure";
        CapabilityBoundingSet = "";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectSystem = "strict";
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
        ];
        RestrictRealtime = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" ];
      };
    };
  };
}
