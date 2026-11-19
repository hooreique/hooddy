{ pkgs, hooddyConfiguration }:
let
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;
  fixtures = [ ./fixtures/bukupan.nix ./fixtures/journal.nix ];
  enabled = { services.bukupan.enable = true; services.buku-journal.enable = true; };
  evaluate = modules: hooddyConfiguration { inherit system modules; };
  fails = args: !(builtins.tryEval (hooddyConfiguration args).activationPackage.drvPath).success;
  invalid = modules: fails { inherit system modules; };
  generation = evaluate (fixtures ++ [ enabled { hooddy.httpPort = 8080; hooddy.httpsPort = 8443; } ]);
  empty = evaluate fixtures;
  disabled = evaluate (fixtures ++ [ enabled { hooddy.enable = false; } ]);
  unit = ''
    [Service]
    ExecStart=${pkgs.coreutils}/bin/sleep infinity
    [Install]
    WantedBy=default.target
  '';
  service = { inherit unit; healthCheck = { url = "http://127.0.0.1:3000/health"; timeoutSeconds = 2; }; };
  checks = [
    (empty.config.hooddy.services == { })
    (empty.config.hooddy.sites == { })
    (builtins.attrNames generation.config.hooddy.services == [ "buku-journal" "bukupan" ])
    (empty.config.hooddy.enable && empty.config.hooddy.httpPort == null && empty.config.hooddy.httpsPort == null)
    (lib.all (target: (hooddyConfiguration { system = target; modules = fixtures ++ [ enabled ]; }).activationPackage.system == target) [ "x86_64-linux" "aarch64-linux" ])
    (lib.all (url: (builtins.tryEval (evaluate [ { hooddy.services.app = service // { healthCheck = { inherit url; timeoutSeconds = 1; }; }; } ]).activationPackage.drvPath).success) [ "http://localhost/health" "http://127.0.0.1/health" "http://[::1]:3000/health" ])
    ((evaluate [ { hooddy.services.app = { inherit unit; }; } ]).config.hooddy.services.app.healthCheck == null)
    ((builtins.tryEval (evaluate [ { hooddy.services.app.unit = "[Service]\nExecStart = ${pkgs.coreutils}/bin/sleep \\\n infinity\n[Install]\nWantedBy = default.target\n"; } ]).activationPackage.drvPath).success)
    ((builtins.tryEval (evaluate [ { hooddy.sites.a.config = "http://same.test { respond a }"; hooddy.sites.b.config = "https://same.test { respond b }"; } ]).activationPackage.drvPath).success)
    ((evaluate [ ({ lib, ... }: { hooddy.sites.foo.config = lib.mkDefault "http://default.test {\n respond default\n}"; }) { hooddy.sites.foo.config = "http://override.test {\n respond override\n}"; } ]).config.hooddy.sites.foo.config == "http://override.test {\n respond override\n}")
    ((hooddyConfiguration { inherit system; specialArgs.extra = "extra"; modules = [ ({ extra, ... }: { hooddy.sites.extra.config = "http://${extra}.test {\n respond extra\n}"; }) ]; }).config.hooddy.sites.extra.config == "http://extra.test {\n respond extra\n}")
    (fails { system = "x86_64-darwin"; modules = [ ]; })
    (lib.all (name: fails { inherit system; modules = [ ]; specialArgs.${name} = null; }) [ "lib" "config" "options" "pkgs" ])
    (invalid [ { hooddy.sites."../escape".config = ""; } ])
    (invalid [ { hooddy.services."bad.service" = service; } ])
    (invalid [ { hooddy.sites.missing = { }; } ])
    (invalid [ { hooddy.services.missing = { }; } ])
    (invalid [ { hooddy.sites.wrong.config = 3; } ])
    (invalid [ { hooddy.httpPort = 0; } ])
    (invalid [ { hooddy.httpsPort = 65536; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.timeoutSeconds = 0; healthCheck.url = "http://127.0.0.1/"; }; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.url = "http://127.0.0.1/"; }; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.url = "https://127.0.0.1/"; healthCheck.timeoutSeconds = 1; }; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.url = "http://example.test/"; healthCheck.timeoutSeconds = 1; }; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.url = "http://127.256.0.1:80/"; healthCheck.timeoutSeconds = 1; }; } ])
    (invalid [ { hooddy.services.app = service // { healthCheck.url = "http://127.0.0.1:65536/"; healthCheck.timeoutSeconds = 1; }; } ])
    (invalid [ { hooddy.services.app.unit = "[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n"; } ])
    (invalid [ { hooddy.services.app.unit = "[Service]\nExecStart=${pkgs.coreutils}/bin/true\n"; } ])
    (invalid [ { hooddy.services.app.unit = "[Service]\nExecStop=${pkgs.coreutils}/bin/true\n[Install]\nWantedBy=default.target\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "Alias=unmanaged.service\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "Also=unmanaged.service\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "RequiredBy=unmanaged.target\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "UpheldBy=unmanaged.target\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "WantedBy=unmanaged.target\n"; } ])
    (invalid [ { hooddy.services.app.unit = unit + "WantedBy=\n"; } ])
    (invalid (fixtures ++ [ enabled { services.buku-journal.port = 3001; } ]))
    (invalid (fixtures ++ [ enabled { services.buku-journal.domain = "bukupan.example.test"; } ]))
    (invalid [ { hooddy.sites.a.config = "same.example.test {\n respond a\n}"; hooddy.sites.b.config = "same.example.test {\n respond b\n}"; } ])
    (invalid [ { hooddy.sites.a.config = "\"same.example.test\" { respond a }"; hooddy.sites.b.config = "same.example.test { respond b }"; } ])
    (invalid [ { hooddy.sites.a.config = "first"; } { hooddy.sites.a.config = "second"; } ])
  ];
in
assert lib.all (check: check) checks;
pkgs.runCommand "hooddy-module-tests" {
  nativeBuildInputs = [ pkgs.python3 pkgs.systemd pkgs.caddy ];
  SYSTEMD_UNIT_PATH = "${pkgs.systemd}/example/systemd/user";
} ''
  export HOME="$TMPDIR/home"
  export XDG_RUNTIME_DIR="$TMPDIR/run"
  mkdir -m 700 "$HOME" "$XDG_RUNTIME_DIR"
  python3 - ${generation.activationPackage} ${empty.activationPackage} ${disabled.activationPackage} <<'PY'
  import json
  import pathlib
  import sys
  generation, empty, disabled = map(pathlib.Path, sys.argv[1:])
  manifest = json.loads((generation / "manifest.json").read_text())
  assert manifest["protocolVersion"] == 2
  assert manifest["enabled"] is True
  assert manifest["caddy"] == "${lib.getExe pkgs.caddy}"
  assert manifest["system"] == "${system}"
  assert sorted(manifest["services"]) == ["buku-journal", "bukupan"]
  assert all((generation / item["unit"]).is_file() for item in manifest["services"].values())
  assert manifest["services"]["bukupan"]["healthCheck"]["url"] == "http://127.0.0.1:3001/"
  config = (generation / "Caddyfile").read_text()
  assert config.index("buku-journal.example.test") < config.index("bukupan.example.test")
  assert "admin unix/{$XDG_RUNTIME_DIR}/hooddy/admin.sock" in config
  assert "http_port 8080" in config and "https_port 8443" in config
  directives = (generation / "units/hooddy.service").read_text().splitlines()
  for directive, command in [("ExecStart", "run"), ("ExecReload", "reload")]:
      line = next(line for line in directives if line.startswith(directive + "="))
      executable, operation, flag, store_config, *args = line.split("=", 1)[1].split()
      assert (executable, operation, flag, args) == (manifest["caddy"], command, "--config", ["--adapter", "caddyfile"])
      assert pathlib.Path(store_config).read_text() == config
  assert "http_port" not in (empty / "Caddyfile").read_text()
  assert json.loads((empty / "manifest.json").read_text())["services"] == {}
  assert [path.name for path in disabled.iterdir()] == ["manifest.json"]
  manifest = json.loads((disabled / "manifest.json").read_text())
  assert manifest["enabled"] is False and manifest["services"] == {} and manifest["caddy"] is None
  PY
  systemd-analyze --user verify ${generation.activationPackage}/units/*.service
  for generation in ${generation.activationPackage} ${empty.activationPackage}; do
    caddy validate --adapter caddyfile --config "$generation/Caddyfile"
  done
  touch "$out"
''
