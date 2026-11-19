# hooddy

hooddy runs Caddy and application services under your systemd user manager.
Declare them in `~/.config/hooddy/flake.nix` and apply them with **`hooddy switch`**.
The first switch installs, enables, and starts the services. Run the same command
after each configuration change.

## Quick start

Use Linux on x86_64 or ARM64 with Nix flakes enabled and a working systemd user
session. Run these commands as the user who will own the services. That user
needs permission to enable linger so the services can run after logout.

```sh
nix profile add github:hooreique/hooddy
mkdir -p ~/.config/hooddy
cd ~/.config/hooddy
```

Save this as `flake.nix`. On ARM64, set `system = "aarch64-linux";`.

```nix
{
  inputs.hooddy.url = "github:hooreique/hooddy";

  outputs = { hooddy, ... }: {
    hooddyConfigurations.default = hooddy.lib.hooddyConfiguration {
      system = "x86_64-linux";
      modules = [{
        hooddy.sites.demo.config = ''
          http://127.0.0.1:8080 {
            bind 127.0.0.1
            respond "Hello from hooddy!"
          }
        '';
      }];
    };
  };
}
```

Apply the flake and request the local site.

```sh
hooddy switch
curl http://127.0.0.1:8080
```

The first switch creates `flake.lock` if needed. Later switches use the pinned
inputs. To change the response, edit the `respond` line and run `hooddy switch`
again. To upgrade the pinned Caddy and module definitions, run `nix flake update` in
`~/.config/hooddy`, review the lock file, and switch.

## Remove the services

Set `hooddy.enable = false;` inside your module and run `hooddy switch`. This
stops and removes Caddy and the managed application units and releases hooddy's
Nix GC roots. Your flake, certificates, and application data stay available for
a future switch. User linger stays enabled for other services that rely on it.

For application modules, diagnostics, and upgrades from 0.1, see the
[contributor guide](CONTRIBUTING.md). [MIT license](LICENSE).
