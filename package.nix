{
  lib,
  writeShellApplication,
  writeText,
  caddy,
  coreutils,
  nix,
  systemd,
  util-linux,
}:
let
  version = "0.1.0";
  script = builtins.replaceStrings [ "@version@" "@caddy@" ] [ version "${caddy}" ] (
    builtins.readFile ./hooddy.sh
  );
in
writeShellApplication {
  name = "hooddy";
  derivationArgs = { inherit version; };
  runtimeInputs = [ coreutils nix systemd util-linux ];
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
