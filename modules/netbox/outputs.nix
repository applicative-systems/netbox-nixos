# the outputs of the served flake; also usable directly on a data.json
{
  data,
  nixpkgs ? null,
}:
let
  named = builtins.filter (h: (h.name or null) != null) (data.devices ++ data.virtual_machines);
  hosts = builtins.listToAttrs (
    map (h: {
      inherit (h) name;
      value = h;
    }) named
  );
  names = builtins.attrNames hosts;
  forHosts =
    f:
    builtins.listToAttrs (
      map (name: {
        inherit name;
        value = f name;
      }) names
    );
  # profiles, not modules: importing one configures the host, there is nothing
  # to enable
  hostProfile = host: import ./default.nix { inherit hosts host; };
in
# listToAttrs would silently keep one of them
if builtins.length named != builtins.length names then
  throw "netbox: two hosts share a name"
else
  {
    lib = {
      inherit data hosts hostProfile;
    };

    nixosProfiles = forHosts hostProfile;

    # only the netbox-derived part of a system; disks, users and the rest can
    # come from the config context or from a flake that imports the module
    nixosConfigurations =
      if nixpkgs == null then
        { }
      else
        forHosts (
          name:
          nixpkgs.lib.nixosSystem {
            modules = [
              (hostProfile name)
              { nixpkgs.hostPlatform = hosts.${name}.custom_fields.nixos_system or "x86_64-linux"; }
            ];
          }
        );
  }
