# hooddy

hooddy manages a shared Caddy server as a systemd user service. Independent
applications provide ordinary Caddyfiles, and Caddy serves their subdomains
through the same public ports.

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
nix profile add github:hooreique/hooddy
hooddy install
hooddy start
```

Installing the CLI into a profile does not install the service. The service's
Caddy executable is retained by a separate Nix GC root, including when the CLI
was invoked with `nix run`.

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

## Commands

| Command | Behavior |
| --- | --- |
| `install` | Require an absent installation; install and enable linger, without enabling or starting the service. |
| `uninstall` | Require an installation; disable, stop, and remove it. |
| `uninstall --disable-linger` | Also disable linger for the user. This affects all of that user's services. |
| `start` | Enable and start; succeed if already running. |
| `stop` | Disable and stop; succeed if already stopped. |
| `restart` | Reload if running, otherwise start; preserve the boot-time enablement setting. |
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
