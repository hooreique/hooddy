{
  runCommand,
  python3,
  bash,
  coreutils,
  util-linux,
  caddy,
  systemd,
  hooddy,
}:
runCommand "hooddy-tests" {
  nativeBuildInputs = [ python3 bash coreutils util-linux systemd ];
  SYSTEMD_UNIT_PATH = "${systemd}/example/systemd/user";
} ''
  python3 ${./test_cli.py} ${hooddy.testScript} ${hooddy}/bin/hooddy ${caddy}/bin/caddy ${./fake_system.py}
  touch "$out"
''
