# hooddy

hooddy manages a shared Caddy server and application services with systemd user
services. Applications can provide ordinary Caddyfiles directly or compose
standalone Nix modules and apply them together with `hooddy switch`.

Version: **0.1.0**. License: [MIT](LICENSE).

## Requirements

- Linux on `x86_64` or `aarch64`
- Nix with flakes enabled
- A working systemd user session and `loginctl`
- Permission to enable linger for the service's user
- Permission to bind the ports used by your sites

Run hooddy as the user who will own the service. Installation enables linger
so the user's services can run without a login session. If local policy denies
that operation, hooddy fails with instructions instead of installing partially.

Ports 80 and 443 may require host-level preparation for an unprivileged user.
hooddy does not change host networking or grant port-binding privileges. For
local use, a site can explicitly choose an unprivileged HTTP port.

Only systemd user services are supported. There are no Darwin packages, system
services, NixOS modules, or Home Manager modules.

## Install and run

```sh
nix run github:hooreique/hooddy -- install
nix run github:hooreique/hooddy -- start
```

`install` creates the service and enables linger. It leaves the service stopped
and disabled. `start` enables it for future user-manager starts and starts it
immediately.

You can also install the CLI into your Nix profile:

```sh
nix profile add github:hooreique/hooddy#hooddy
hooddy install
hooddy start
```

Installing the CLI into a profile does not install the service. The service's
Caddy executable is retained by a separate Nix GC root, including when the CLI
was invoked with `nix run`.

If a gateway forwards external port 80 to local port 8080 and external port 443
to local port 8443, install with:

```sh
hooddy install --http-port 8080 --https-port 8443
hooddy start
```

These install-only options set Caddy's global `http_port` and `https_port`.
Each accepts a port from 1 to 65535; you can specify either or both. Omitted
options leave Caddy's defaults unchanged. Site addresses still use ordinary
domains without the internal port, and automatic HTTPS remains enabled.

Port options require a new main Caddyfile. If one already exists, hooddy fails
without changing it; edit its existing global options block directly instead:

```caddyfile
{
    admin unix/{$XDG_RUNTIME_DIR}/hooddy/admin.sock
    http_port 8080
    https_port 8443
}

import sites.d/*.caddy
```

For an installed service, apply that edit with `hooddy restart`.

## Add sites

The default configuration directory is `~/.config/hooddy`. When
`XDG_CONFIG_HOME` is set, it is `$XDG_CONFIG_HOME/hooddy` instead.

```text
~/.config/hooddy/
├── Caddyfile
├── caddy -> /nix/store/…-caddy-…
└── sites.d/
    ├── foo.caddy
    └── bar.caddy
```

For example, `sites.d/foo.caddy` can contain:

```caddyfile
foo.hooreique.com {
    reverse_proxy 127.0.0.1:3000
}
```

And `sites.d/bar.caddy` can contain:

```caddyfile
bar.hooreique.com {
    reverse_proxy 127.0.0.1:4000
}
```

Point both DNS records at the server. These addresses use Caddy's automatic
HTTPS and HTTP-to-HTTPS redirects. For an explicitly HTTP-only site, use an
address such as `http://foo.hooreique.com:80`.

Apply added, edited, or removed files with:

```sh
hooddy restart
```

Applications only need to write their Caddyfile to the directory. They do not
need the hooddy executable, a registration command, or any additional metadata.
Files may exist before hooddy is installed. Replace files atomically, using a
temporary name that does not end in `.caddy`, to avoid loading partial writes.
The operator applies changes explicitly; hooddy does not watch the directory.

The main Caddyfile imports `sites.d/*.caddy`. Site files contain ordinary site
blocks, including any static-file or proxy directives the application needs.
Keep shared global options in the main Caddyfile. No sites is also a valid
configuration.

The generated main Caddyfile uses a private admin socket at
`$XDG_RUNTIME_DIR/hooddy/admin.sock` for reloads. Keep the admin endpoint enabled.
An existing main Caddyfile is preserved when installing or reinstalling.

## Configure applications with modules

The module system is standalone; it does not require NixOS or Home Manager.
There is one configuration per user, in
`${XDG_CONFIG_HOME:-$HOME/.config}/hooddy/flake.nix`. Install hooddy first, then
add this line to the main `Caddyfile`, outside its global options block:

```caddyfile
import generated/sites.caddy
```

Keep the existing `import sites.d/*.caddy` line if you also have ordinary site
files. The main file continues to own global options, ports, and the admin
socket. Switch requires this explicit import and never adds it automatically.
The first successful switch creates `generated/sites.caddy`; apply that switch
before restarting Caddy with the new import.

This self-contained flake runs a small local site. Use `aarch64-linux` instead
of `x86_64-linux` on an ARM64 host:

```nix
{
  inputs.hooddy.url = "github:hooreique/hooddy";

  outputs = { hooddy, ... }: {
    hooddyConfigurations.default = hooddy.lib.hooddyConfiguration {
      system = "x86_64-linux";
      modules = [
        ({ pkgs, ... }:
          let
            content = pkgs.writeTextDir "index.html" "Hello from hooddy!";
          in {
            hooddy.services.demo = {
              unit = ''
                [Unit]
                Description=Hooddy demo

                [Service]
                ExecStart=${pkgs.python3}/bin/python3 -m http.server 3001 --bind 127.0.0.1 --directory ${content}
                Restart=on-failure

                [Install]
                WantedBy=default.target
              '';
              healthCheck = {
                url = "http://127.0.0.1:3001/";
                timeoutSeconds = 10;
              };
            };
            hooddy.sites.demo.config = ''
              http://localhost:8080 {
                bind 127.0.0.1
                reverse_proxy 127.0.0.1:3001
              }
            '';
          })
      ];
    };
  };
}
```

Create the input lock explicitly, then apply the configuration:

```sh
cd "${XDG_CONFIG_HOME:-$HOME/.config}/hooddy"
nix flake lock
hooddy switch
```

Visit `http://localhost:8080`. Switch builds
`hooddyConfigurations.default.activationPackage`, validates the complete Caddy
configuration with the installed Caddy executable, verifies the generated user
units, and enables and starts the declared application services. HTTP health
checks must return a 2xx response before their timeout. Without a health check,
hooddy checks that the service is active.

Switch requires an existing installation, a working systemd user manager, and
enabled linger. If linger is disabled, follow the reported `loginctl
enable-linger` instruction; switch does not change it. Caddy starts if needed,
and its existing boot-time enablement setting is preserved. To enable Caddy
at boot, use `hooddy start`.

Applying the same configuration again repairs missing unit files and starts
stopped application services. Healthy, unchanged services keep running. Removing
an application from the configuration stops and disables its managed service.
The generated site file is ordered by name; a configuration with no sites
produces an empty file.

Switch holds the hooddy lock across its build and activation and never updates
`flake.lock`. To upgrade module inputs, run `nix flake update` in the configuration
directory, review the changes, then run `hooddy switch`. Updating the CLI's Nix
profile changes the CLI; updating the configuration's hooddy input changes its
module evaluator and generation. The CLI checks the generation protocol version
and target system before applying it.

Configuration is evaluated from a snapshot using a `path:` flake reference,
which includes untracked local files. Keep secrets outside the configuration
directory and out of module options. The Nix store and generated unit and site
text are readable by local users. Applications should read credentials from
protected runtime files. Hooddy does not create, migrate, or delete application
data or credentials.

### Application module interface

An application can export `hooddyModules.default`, declare its public options
under `services.<app>`, and contribute these protocol v1 options:

| Option | Value |
| --- | --- |
| `hooddy.sites.<name>.config` | A required string containing complete Caddy site blocks. |
| `hooddy.services.<name>.unit` | A required string containing a complete systemd user unit. |
| `hooddy.services.<name>.healthCheck` | `null`, or `{ url; timeoutSeconds; }` with a loopback HTTP URL and positive timeout in seconds. |

Names must match `[a-zA-Z0-9][a-zA-Z0-9_-]*`. Each service becomes
`hooddy-<name>.service`; these unit names are reserved for hooddy. Units must
include `[Install]` with `WantedBy=default.target` and use absolute Nix store
paths for executables. There are no activation hooks. Modules can provide sites,
services, or both. Escape application option values appropriately when placing
them in Caddy or systemd text.

`hooddy.lib.hooddyConfiguration` takes `system`, `modules`, and optional
`specialArgs` (default `{}`), and returns `config`, `options`, and
`activationPackage`. Modules receive `lib`, `config`,
`options`, and the selected system's `pkgs`; `specialArgs` cannot replace those
arguments. Normal Nix module merging applies and imports must be static.
Application modules should remain disabled until explicitly enabled and choose
packages using `pkgs.stdenv.hostPlatform.system`, with a clear error for
unsupported systems. Following `hooddy/nixpkgs` in an application's flake input
is recommended but optional.

See [the module design](hooddy-module-design.md) for a configuration combining
application modules. Statically recognizable port and domain collisions fail
evaluation; full Caddy validation and actual socket binds remain authoritative.
DNS, certificate reachability, and permission to bind privileged ports remain
operator responsibilities.

### Migration and failed switches

Switch does not adopt existing application units or site files. Before migrating,
back up the existing configuration, stop and disable the old application units,
and remove their old unit and site files. Keep application data and credentials
in place. Then enable the corresponding modules and switch. Existing units or
sites can otherwise cause duplicate listeners or domains.

Only `generated/sites.caddy` and the declared `hooddy-*.service` files are
managed application destinations. Ownership is tracked using the previous
manifest and file hashes. Switch refuses conflicting files, symlinks, or edits
to managed files; a management comment alone does not establish ownership.
Change module definitions and switch instead of editing generated files.

Activation retains the current and immediately previous generations as Nix GC
roots under `hooddy/.switch/roots/`. Its durable journal is
`hooddy/.switch/journal.json`, and `hooddy/.switch/current.json` records the
committed generation. A failed activation restores the previous managed files
and service states. After an interruption, the next invocation recovers the
previous generation before accepting another switch. If rollback also fails,
hooddy reports both errors and retains the journal and generation roots for
recovery; resolve the reported cause and retry. Do not delete these files or
roots while recovery is pending.

Changed backends may briefly be unavailable during activation, and Caddy
reloads can close WebSockets. Recovery cannot undo application data writes or
external effects.

## Commands

| Command | Behavior |
| --- | --- |
| `install [--http-port PORT] [--https-port PORT]` | Require an absent installation; install and enable linger, without enabling or starting the service. |
| `uninstall` | Require an installation; disable, stop, and remove it. |
| `uninstall --disable-linger` | Also disable linger for the user. This affects all of that user's services. |
| `start` | Enable and start; succeed if already running. |
| `stop` | Disable and stop; succeed if already stopped. |
| `restart` | Reload if running, otherwise start; preserve the boot-time enablement setting. |
| `switch` | Build and apply the locked module configuration; require enabled linger and preserve Caddy boot-time enablement. |
| `--help`, `-h` | Show usage. Each command also accepts `--help`. |
| `--version`, `-V` | Print the CLI version. |

Every service command except `install` requires an existing installation.
Installing twice, uninstalling twice, or operating on an absent installation
fails with an explanation and exit code **1**. `start`, `stop`, and `restart`
are idempotent. `start` and `restart` restore linger if it was disabled after
installation.

Reloads use Caddy's configuration loading: an invalid configuration returns a
failure and the running configuration stays active. The invalid file remains
on disk; fix or remove it before retrying or starting a stopped service.
Reloads can close existing WebSocket connections; individual proxy settings
such as `stream_close_delay` can delay that closure.

For service status and logs:

```sh
systemctl --user status hooddy.service
journalctl --user -u hooddy.service
```

## Removal and reinstallation

If you use modules, first disable or remove all application services in the flake
and run `hooddy switch`. Uninstall refuses while switch-managed application
services remain. Application data stays in place.

```sh
hooddy uninstall
```

Uninstallation removes `$XDG_CONFIG_HOME/systemd/user/hooddy.service` and the
`hooddy/caddy` GC root. It preserves the main Caddyfile, site files, and Caddy's
certificate data. Linger remains enabled unless `--disable-linger` is supplied.

Certificate data uses Caddy's usual `$XDG_DATA_HOME/caddy` location, defaulting
to `~/.local/share/caddy`. The configuration and data paths are captured in the
unit at installation time. Use the same `XDG_CONFIG_HOME` for subsequent CLI
commands and for the systemd user manager's configuration search path.

To replace the installed Caddy version, uninstall and then install using the
desired hooddy package, followed by `start`. Configuration and certificates
survive this sequence; the service is unavailable between stop and start.
