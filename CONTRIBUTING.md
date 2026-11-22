# Contributing to hooddy

## Development

From the repository root on Linux, run the flake checks.

```sh
nix develop
nix flake check
```

The checks exercise module evaluation, generated Caddy and systemd files, and
CLI activation and recovery with isolated service-manager substitutes.
See the [module checks](tests/modules.nix) and [CLI checks](tests/package.nix).

## Configuration

The configuration entry point is `~/.config/hooddy/flake.nix`. Use
`hooddy.lib.hooddyConfiguration { system = …; modules = [ … ]; }` to define
`hooddyConfigurations.default`. Modules can be inline, imported from local
files, or supplied by another flake input. Supported systems are
`x86_64-linux` and `aarch64-linux`. The [quick start](README.md) contains
a complete one-file example.

| Option | Effect |
| --- | --- |
| `hooddy.enable` | Defaults to `true`. Set to `false` and switch to remove the managed deployment, including Caddy. |
| `hooddy.httpPort`, `hooddy.httpsPort` | Set Caddy's internal HTTP and HTTPS ports. Each defaults to `null` to use Caddy's default. |
| `hooddy.sites.<name>.config` | Complete Caddy site blocks as a string. hooddy combines the sites in name order. |
| `hooddy.services.<name>.unit` | A complete systemd user unit, installed as `hooddy-<name>.service`. |
| `hooddy.services.<name>.healthCheck` | An optional `{ url; timeoutSeconds; }` specifying a loopback HTTP GET that must return 2xx before the timeout. The default `null` checks that the unit is active. |

For example, a gateway forwarding public ports 80 and 443 to unprivileged
local ports can use `hooddy.httpPort = 8080;` and `hooddy.httpsPort = 8443;`.
Specify the public domain in the site block. The operator configures DNS and
port forwarding.

### Writing an application module

An application flake can export `hooddyModules.default` and define its own
options under `services.<app>`. The user's flake can then import the module
and enable the app in its `modules` list. For example, given a `myApp` input
with a module defining `services.myApp.enable`, use the following expression.

```nix
modules = [
  myApp.hooddyModules.default
  { services.myApp.enable = true; }
];
```

A module receives `lib`, `config`, `options`, and the target platform's `pkgs`.
Pass additional arguments through `specialArgs`. Use `lib.mkIf` to contribute
sites and units when the application is enabled. Choose names matching
`[a-zA-Z0-9][a-zA-Z0-9_-]*`. Service executables must have absolute Nix store
paths and the unit must have `[Install]` with `WantedBy=default.target`.
The [example application module](tests/fixtures/application.nix) defines a
backend, its health check, and a reverse proxy.

Inside a Git worktree, `hooddy switch` snapshots the current contents of
files tracked under the configuration directory, including unstaged edits.
Use `git add` to include new source files such as `flake.nix`. Deleted files
and submodules are skipped. Outside a Git worktree, the snapshot includes
untracked source files. An existing `flake.lock` is included either way.
Keep credentials outside that directory and use protected runtime files for
secrets. Paths and text included in a generation are readable in the Nix store.

## Switching and recovery

`hooddy switch` creates `flake.lock` when the configuration has none. It builds
`hooddyConfigurations.default.activationPackage` using the locked inputs.
When you change the flake's input declarations, run `nix flake lock` in the
configuration directory to bring the existing lock up to date before switching.
The generation supplies the Caddy binary,
complete Caddyfile, and systemd user units. Switch validates the configuration
and units, enables the declared services, and checks application readiness.
Removing an app from the flake stops and removes its managed unit on the next
switch. To inspect the shared server, use systemd directly.

```sh
systemctl --user status hooddy.service
journalctl --user -u hooddy.service
```

Change module definitions to edit a managed unit. Switch checks file ownership
and reports conflicting units or local edits. A failed activation rolls back
managed files and service states. If an activation was interrupted, the next
switch recovers it first. If recovery reports a conflict, resolve its cause
and retry with the recorded journal and generation roots in place.

## Moving from 0.1

Before upgrading the CLI, back up the old hooddy configuration and translate
any manually maintained Caddy sites into `hooddy.sites`. If 0.1 manages
application units, remove those declarations from the **old** flake and run
the **0.1** `hooddy switch` to retire them. Then run the **0.1** `hooddy
uninstall` to retire the old Caddy service. This stops the old deployment, so
allow for downtime before the first 0.2 switch.

Move the old configuration directory, including its hidden switch state, to a
backup location outside `~/.config/hooddy`. Version 0.1 used
`$XDG_CONFIG_HOME/hooddy` when that variable was set. Create the 0.2 flake at
`~/.config/hooddy/flake.nix`, update its hooddy input to 0.2, and follow the
quick start with the new CLI. Caddy reuses certificates in its user data
directory when the service runs as the same user with the same data path.
