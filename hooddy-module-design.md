# hooddy module protocol design

hooddy provides a standalone Nix module system for a user's applications and
shared Caddy server. It is neither
a NixOS nor a Home Manager module. v1 supports `x86_64-linux` and
`aarch64-linux` and systemd user services.

## User experience

The user installs `nix profile add github:hooreique/hooddy#hooddy`, defines
`${XDG_CONFIG_HOME:-$HOME/.config}/hooddy/flake.nix`, and runs `hooddy switch`.
The flake lock pins all inputs; switch never updates it. Updating the profile
changes the CLI, while updating the flake's hooddy input changes the evaluator
and generation. The CLI checks the generation protocol version before applying
it. Run `nix flake update` in the config directory to upgrade inputs explicitly.

The exact output is `hooddyConfigurations.default.activationPackage`, a
store derivation. There is one configuration per user in v1. Switch builds
`path:<config-dir>#hooddyConfigurations.default.activationPackage` without
changing the lock. The `path:` reference includes untracked local files; keep
secrets out of this directory. `system` is explicit and checked against the
host, rather than determined through impure evaluation.

```nix
{
  inputs = {
    hooddy.url = "github:hooreique/hooddy";
    bukupan.url = "github:hooreique/bukupan";
    buku-journal.url = "github:hooreique/buku-journal";
    bukupan.inputs.nixpkgs.follows = "hooddy/nixpkgs";
    buku-journal.inputs.nixpkgs.follows = "hooddy/nixpkgs";
  };
  outputs = { hooddy, bukupan, buku-journal, ... }: {
    hooddyConfigurations.default = hooddy.lib.hooddyConfiguration {
      system = "x86_64-linux";
      modules = [
        bukupan.hooddyModules.default
        buku-journal.hooddyModules.default
        ({ ... }: {
          services.bukupan = {
            enable = true;
            domain = "bukupan.example.com";
            port = 3001;
          };
          services.buku-journal = {
            enable = true;
            domain = "journal.example.com";
            port = 3002;
          };
        })
      ];
    };
  };
}
```

`hooddy.lib.hooddyConfiguration { system, modules, specialArgs ? {} }`
evaluates hooddy's built-in module and `modules` using `lib.evalModules`, and
returns `{ config, options, activationPackage; }`. Modules receive `lib`,
`config`, `options`, and `pkgs` for the selected system; `specialArgs` adds
arguments but cannot override those reserved names. Imports are static. An app
exports a system-independent `hooddyModules.default`, selects its package for
`pkgs.stdenv.hostPlatform.system`, and fails clearly if unsupported. Following
hooddy's nixpkgs is recommended, not required. Importing an app module alone
does not enable it.

## Module protocol v1

Apps define their public options under `services.<app>` and contribute to the
following shared options. The names and types are the protocol; each app owns
its own public options and package. Normal Nix module merging applies.

| Option | Type and meaning |
| --- | --- |
| `hooddy.sites.<name>.config` | Required string containing complete, ordinary Caddy site blocks. |
| `hooddy.services.<name>.unit` | Required string containing a complete systemd user unit. |
| `hooddy.services.<name>.healthCheck` | Nullable `{ url; timeoutSeconds; }`; HTTP GET must return 2xx within the positive timeout. Null checks only active state. |

Names match `[a-zA-Z0-9][a-zA-Z0-9_-]*`; unit names are
`hooddy-<name>.service` and reserved for hooddy. Units include `[Install]`
`WantedBy=default.target`, use absolute store paths for executables, and have
no arbitrary activation hooks. Health URLs use loopback HTTP. Switch enables
and starts declared services; removing one stops and disables it. A module can
declare a site, a service, or both. Site files are ordinary Caddyfiles, and
applications need neither the hooddy CLI nor runtime registration metadata.

Bukupan's module derives its backend listen address and site proxy from
one host and port, serve its packaged static client, and declare its health
check. Journal's module does the same for its backend, preserving its XDG
data defaults, token and Codex authentication. App modules escape values for
systemd and Caddy. Statically detectable duplicate ports or domains should fail
evaluation; Caddy validation and actual binds remain authoritative. DNS,
certificate reachability and privileged port preparation remain operator tasks.

The Nix store and generated unit/site text are readable by local users. Never
put credentials in module options; applications use existing protected runtime
files for secrets. hooddy does not create, migrate, or delete application data
or credentials.

## Existing hooddy integration

`switch` requires an existing installation, a working systemd user manager,
and enabled linger. It reports how to enable linger but does not change it.
Existing `install`/`uninstall` strict preconditions and idempotent
`start`/`stop`/`restart` remain. Switch starts Caddy if needed and preserves its
prior boot-time enablement, like `restart`. Its installed executable and GC root
remain controlled by install/uninstall; to update Caddy, use the existing
uninstall/install procedure. Validation and reload use that same executable.

The existing main Caddyfile and `sites.d` remain user-owned. The operator adds
one explicit import to the main Caddyfile:

```caddyfile
import generated/sites.caddy
```

The main Caddyfile continues to own global settings, including the admin socket
and `http_port`/`https_port`. Switch generates
`$XDG_CONFIG_HOME/hooddy/generated/sites.caddy` in deterministic name order;
an empty configuration produces an empty import file. It validates the complete
candidate configuration, including ordinary `sites.d` files, with the
installed Caddy. A missing import is an error with instructions, not an
automatic edit. The generated file and `hooddy-*.service` units are the only
switch-owned mutable destinations.

Switch does not adopt existing bukupan or journal units or sites. An operator
backs them up, stops and removes the old managed units and site files, then
switches, avoiding duplicate domains and ports; app data stays in place. For
uninstall, switch first to a configuration with no app services. Uninstall
refuses if switch-managed services remain, and otherwise retains its existing
behavior. Modules provide build-time composition, while runtime sites remain
ordinary Caddyfiles.

## Generation and activation

The immutable activation package contains protocol version `1`, target system,
generated site file, units and a machine-readable manifest of managed names and
health checks. Its closure retains app executables and static assets. Switch
rejects incompatible protocol versions or host systems before mutation.

1. Hold the existing hooddy lock across evaluation and activation. Snapshot
   the config for one evaluation, build with the lock unchanged, and retain the
   result with a temporary GC root. Validate the full candidate Caddy config
   using a temporary equivalent import layout, and verify user units with
   systemd. Check names, paths and health URLs. Refuse unmanaged destination
   files, symlinks, unexpected changes to managed files, or unit conflicts.
   Recorded file hashes and the previous manifest establish ownership; a marker
   alone is insufficient. Never overwrite or remove unmanaged files.
2. Keep the previous generation rooted and record active/enabled states. Stop
   changed and removed app services as needed, atomically replace managed unit
   files, daemon-reload, enable/start new and changed services, and wait for
   health checks and active state. Unchanged healthy units keep running.
3. Atomically replace the generated site file and reload or start Caddy with
   the full candidate configuration. Reload failure leaves Caddy's previous
   running configuration active. Only after all checks succeed, atomically
   record the new current generation and its GC root, remove obsolete managed
   files, and retain the immediately previous generation as a rollback root.
   Repeating switch reconciles stopped or missing managed services and checks
   health without restarting unchanged healthy ones.

Activation cannot be atomic across processes: changed backends may be briefly
unavailable and Caddy reloads can close WebSockets. On failure after mutation,
restore old managed files, daemon-reload, restore recorded active/enabled
states and restore/reload the previous Caddy configuration. Report both the
original error and any rollback failures; keep both roots and recovery details
until resolved. First activation failure restores the pre-switch absence of
managed files and services. A durable journal records old and candidate roots
and each phase before mutation. Following interruption, the next invocation
recovers the previous generation before accepting a new switch. Recovery does
not undo app data writes or external effects.

## Validation

Use fixture modules resembling both apps. Test both supported systems, merging and invalid definitions,
locks, GC roots, reconciliation, collisions, additions/changes/removals,
health and Caddy failures, rollback failure, interrupted recovery, and migration
conflicts. Verify full Caddy validation with existing sites and empty/populated
generated files. Use the existing isolated system command substitutes and
temporary Caddy; never operate the real user's services.
