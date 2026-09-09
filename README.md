# netbox-nixos

**This is a proof of concept. Do not use it in production.** It exists to
show that the idea works; see the limitations at the end.

Serve a NetBox instance as a Nix flake, so that NixOS hosts take their network
configuration from the source of truth instead of repeating it.

```nix
inputs.netbox.url = "https://netbox.example.org/nix/flake.tar.gz";

nixosConfigurations.router = nixpkgs.lib.nixosSystem {
  modules = [ netbox.nixosProfiles.router ./hardware.nix ];
};
```

The flake is not a repository. A small service queries NetBox's GraphQL API,
bundles the response as `data.json` with the Nix modules that interpret it,
and serves the bundle as a tarball. Its outputs are `nixosProfiles.<host>`
(profiles, not modules: importing one configures that host, there is nothing
to enable), `nixosConfigurations.<host>` when a nixpkgs input is declared,
and `lib.data`. The mutable URL answers with a
`Link: <…>; rel="immutable"` header ([lockable HTTP tarball protocol]), so
Nix locks a revision the way it locks a Git commit, with a `narHash` it
computes itself. Nix caches the mutable URL for `tarball-ttl` (an hour);
`nix flake update netbox --refresh` bypasses that.

[lockable HTTP tarball protocol]: https://nix.dev/manual/nix/stable/protocols/tarball-fetcher

## What is mapped

Hosts are the devices and virtual machines with platform slug `nixos`.

| NetBox | NixOS |
| --- | --- |
| name | `networking.hostName`, `networking.domain` if it is an fqdn |
| primary IPs of all hosts, addresses with a DNS name | `networking.hosts` |
| enabled physical interface | `systemd.network.networks."10-<name>"`: match by MAC (or name), MTU, addresses |
| virtual interface with a parent and an untagged VLAN | `systemd.network.netdevs."10-<name>"` of kind `vlan` |
| services | `networking.firewall.allowed{TCP,UDP}Ports` |
| config context key `nixos` | merged into the configuration as is |

The config context is the escape hatch: anything NetBox cannot model becomes
a JSON object under `nixos`, weighted and scoped by NetBox's own rules. That
includes routes: relating an address to its prefix is IP arithmetic, which
Nix does not have, so a default route is one line of config context.

## Running the server

```nix
{
  imports = [ netbox-nixos.nixosModules.server ];
  services.netbox-nixos = {
    enable = true;
    netboxUrl = "https://netbox.example.org";
    tokenFile = "/run/secrets/netbox-token";
    publicUrl = "https://netbox.example.org/nix";
    nixpkgs = "github:NixOS/nixpkgs/nixos-25.11";
  };
  services.nginx.virtualHosts."netbox.example.org".locations."/nix/".proxyPass =
    "http://127.0.0.1:8080/";
}
```

## Consuming the flake

As profiles, in a flake that builds the systems itself (see the top of this
file). As configurations, when the server declares a nixpkgs input; let it
follow yours, which also pins it:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    netbox.url = "https://netbox.example.org/nix/flake.tar.gz";
    netbox.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { nixpkgs, netbox, ... }:
    {
      nixosConfigurations = nixpkgs.lib.mapAttrs (
        name: system: system.extendModules { modules = [ ./hosts/${name}.nix ]; }
      ) netbox.nixosConfigurations;
    };
}
```

Or directly, when the config context supplies everything else a system needs
(`fileSystems`, `boot.loader`, `system.stateVersion`):

```
nixos-rebuild switch --flake https://netbox.example.org/nix/flake.tar.gz#router --no-write-lock-file
```

## Tests

`nix flake check`: unit tests, the module evaluated on a recorded NetBox
response, `ruff`, `mypy --strict`, `nixfmt`, and a VM test with a real NetBox
in which Nix itself fetches, locks and evaluates the served flake, before and
after a change in NetBox.

## Demo

```
$ nix run .#demo
>>> netbox.start(); server.start()
>>> netbox.wait_for_unit("netbox-seed.service")
```

NetBox is then at <http://localhost:8001> (admin / admin) and the served
flake at <http://localhost:8080/flake.tar.gz>, declaring the nixpkgs this
repository pins as its input, so evaluating it fetches nothing:

```
$ nix eval 'http://localhost:8080/flake.tar.gz#nixosConfigurations.router.config.systemd.network.networks."10-eth0".address' --refresh --no-write-lock-file
[ "10.0.0.5/24" ]
```

`--refresh` because Nix caches the mutable url for `tarball-ttl`,
`--no-write-lock-file` because the served flake declares nixpkgs without
locking it. Change the address in NetBox and evaluate again.

The seed also carries two config contexts, a platform baseline and a heavier
one for the site Kraków, so `time.timeZone`, `services.openssh.enable` and the
default route of `10-eth0` come from JSON in NetBox; edit the context and
evaluate again.

## Limitations

- Revisions live in memory; after a restart, lock files pointing at old
  revisions get a 404 until `nix flake update`.
- The HTTP server is Python's `http.server`; put it behind a reverse proxy.
- With a nixpkgs input declared, the served flake carries no lock for it.
- NetBox 4.3 to 4.6; tested against 4.6.8, the version in nixpkgs.

## What could be done next

- NetBox 4.7 (`port_mappings` on services).
- Revisions on disk, with a retention policy.
- Refresh on NetBox event rules instead of on a timer.
- A `flake.lock` that pins nixpkgs.
- Authentication for consumers (Nix reads `netrc-file` for tarball inputs).
- Routes, once the server relates addresses to prefixes.
- Bonds, bridges, VRFs, tunnels; DHCP and DNS from prefixes and `dns_name`.
- Committing the flake to a Git repository instead of serving it.
