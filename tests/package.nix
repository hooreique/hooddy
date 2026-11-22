{
  runCommand,
  python3,
  bash,
  coreutils,
  caddy,
  git,
  systemd,
  hooddy,
}:
runCommand "hooddy-tests" {
  nativeBuildInputs = [ python3 bash coreutils git systemd ];
  SYSTEMD_UNIT_PATH = "${systemd}/example/systemd/user";
} ''
  python3 ${./test_cli.py} ${hooddy}/bin/hooddy
  python3 ${./test_switch.py} ${hooddy.testScript} ${hooddy}/bin/hooddy ${caddy}/bin/caddy ${./fake_switch.py}
  touch "$out"
''
