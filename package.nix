{
  lib,
  writeShellApplication,
  writeText,
  coreutils,
  nix,
  python3,
  systemd,
}:
let
  version = "0.2.0";
  script = builtins.replaceStrings
    [ "@version@" "@python@" "@switch@" ]
    [ version "${python3}/bin/python3" "${./hooddy-switch.py}" ] (
    builtins.readFile ./hooddy.sh
  );
in
writeShellApplication {
  name = "hooddy";
  derivationArgs = { inherit version; };
  runtimeInputs = [ coreutils nix systemd ];
  text = script;

  passthru.testScript = writeText "hooddy-test-script" script;

  meta = {
    description = "Manage a shared Caddy service with user-level systemd";
    homepage = "https://github.com/hooreique/hooddy";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" "aarch64-linux" ];
    mainProgram = "hooddy";
  };
}
