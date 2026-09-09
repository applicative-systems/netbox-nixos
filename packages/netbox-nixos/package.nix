{
  lib,
  python3Packages,
  modules,
}:
python3Packages.buildPythonApplication {
  pname = "netbox-nixos";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./netbox_nixos.py
      ./tests
    ];
  };
  build-system = [ python3Packages.setuptools ];

  postInstall = ''
    mkdir -p $out/share/netbox-nixos
    cp -r ${modules} $out/share/netbox-nixos/modules
  '';
  # the wrapper tells the program where the modules it serves live
  makeWrapperArgs = [
    "--set-default NETBOX_NIXOS_MODULES ${placeholder "out"}/share/netbox-nixos/modules"
  ];

  nativeCheckInputs = [ python3Packages.unittestCheckHook ];
  unittestFlagsArray = [
    "-s"
    "tests"
    "-v"
  ];
  pythonImportsCheck = [ "netbox_nixos" ];

  meta = {
    description = "serve netbox as a nix flake";
    mainProgram = "netbox-nixos";
  };
}
