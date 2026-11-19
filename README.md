# hooddy

hooddy runs a shared Caddy server as a systemd user service.

## Quick start

You need Linux (x86_64 or ARM64), Nix with flakes enabled, and a working
systemd user session. Run the commands as the user who will own the service.
Installation requires permission to enable linger for that user.

```sh
nix run github:hooreique/hooddy -- install
nix run github:hooreique/hooddy -- start
```

Save this example as `~/.config/hooddy/sites.d/demo.caddy`. If you set
`XDG_CONFIG_HOME`, use `$XDG_CONFIG_HOME/hooddy/sites.d/demo.caddy`.

```caddyfile
http://localhost:8080 {
    bind 127.0.0.1
    respond "Hello from hooddy!"
}
```

Apply the site and open `http://localhost:8080`.

```sh
nix run github:hooreique/hooddy -- restart
```

## Configure a site with Nix

Save these two files in `~/.config/hooddy/` (or `$XDG_CONFIG_HOME/hooddy/`).
Use `aarch64-linux` on ARM64.

**`~/.config/hooddy/flake.nix`**

```nix
{
  inputs.hooddy.url = "github:hooreique/hooddy";

  outputs = { hooddy, ... }: {
    hooddyConfigurations.default = hooddy.lib.hooddyConfiguration {
      system = "x86_64-linux";
      modules = [ ./config.nix ];
    };
  };
}
```

**`~/.config/hooddy/config.nix`**

```nix
{
  hooddy.sites.demo.config = ''
    http://localhost:8081 {
      bind 127.0.0.1
      respond "Hello from hooddy!"
    }
  '';
}
```

Add `import generated/sites.caddy` to the main `Caddyfile` outside its global
options block. Then lock the inputs and run the first switch, which creates the
imported file.

```sh
cd "${XDG_CONFIG_HOME:-$HOME/.config}/hooddy"
nix flake lock
nix run github:hooreique/hooddy -- switch
```

Open `http://localhost:8081`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for Nix modules, service commands, and
development checks. [MIT license](LICENSE).
