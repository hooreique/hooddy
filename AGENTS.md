# AGENTS.md

`README.md` is for installation and everyday use. Keep development guidance here.

- Work in `nix develop`.
- Support only `x86_64-linux` and `aarch64-linux`, using the local `forAllSys`
  helper. Do not add flake-utils, flake-parts, NixOS modules, or Home Manager modules.
- Keep site files ordinary Caddyfiles. Applications must not depend on the hooddy
  CLI or on additional registration metadata.
- Preserve the strict install/uninstall preconditions and idempotent
  start/stop/restart behavior documented in the README.
- Stage new files with `git add` before evaluating the flake so Nix can see them.
- Do not install or operate the real user's service to test changes. The tests
  use isolated systemd/logind/Nix substitutes and a temporary local Caddy process.

## Validation

- Shell lint: `nix develop --command -- shellcheck hooddy.sh`
- Package build: `nix build .#hooddy`
- CLI lifecycle and real Caddy checks: `nix flake check`
- Evaluate both supported platforms: `nix flake show --all-systems`

`package.nix` substitutes the version and Caddy store path into `hooddy.sh`.
Tests execute that same script with isolated system command substitutes; the
packaged executable is also checked for help and version output.
