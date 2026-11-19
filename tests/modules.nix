{ pkgs, hooddyConfiguration }:
let
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;
  fixtures = [ ./fixtures/bukupan.nix ./fixtures/journal.nix ];
  enabled = { services.bukupan.enable = true; services.buku-journal.enable = true; };
  evaluate = modules: hooddyConfiguration { inherit system modules; };
  fails = args: !(builtins.tryEval (hooddyConfiguration args).activationPackage.drvPath).success;
  invalid = modules: fails { inherit system modules; };
  generation = evaluate (fixtures ++ [ enabled ]);
  empty = evaluate fixtures;
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
    (builtins.attrNames generation.options.hooddy == [ "services" "sites" ])
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
  python3 - ${generation.activationPackage} ${empty.activationPackage} <<'PY'
  import json
  import pathlib
  import sys
  generation, empty = map(pathlib.Path, sys.argv[1:])
  manifest = json.loads((generation / "manifest.json").read_text())
  assert manifest["protocolVersion"] == 1
  assert manifest["system"] == "${system}"
  assert sorted(manifest["services"]) == ["buku-journal", "bukupan"]
  assert all((generation / item["unit"]).is_file() for item in manifest["services"].values())
  assert manifest["services"]["bukupan"]["healthCheck"]["url"] == "http://127.0.0.1:3001/"
  sites = (generation / "sites.caddy").read_text()
  assert sites.index("buku-journal.example.test") < sites.index("bukupan.example.test")
  assert "/nix/store/" in sites
  assert (empty / "sites.caddy").read_text() == ""
  assert json.loads((empty / "manifest.json").read_text())["services"] == {}
  PY
  systemd-analyze --user verify ${generation.activationPackage}/units/*.service
  for generation in ${generation.activationPackage} ${empty.activationPackage}; do
    cat > "$TMPDIR/Caddyfile" <<EOF
  {
    admin off
  }
  import $generation/sites.caddy
  EOF
    caddy validate --adapter caddyfile --config "$TMPDIR/Caddyfile" > "$TMPDIR/caddy.log" 2>&1 || { cat "$TMPDIR/caddy.log"; exit 1; }
  done
  touch "$out"
''
