{ lib, ... }:
let
  inherit (lib) mkOption types;
in
{
  options.hooddy = {
    sites = mkOption {
      default = { };
      description = "Ordinary Caddy site blocks, composed in name order.";
      type = types.attrsOf (types.submodule {
        options.config = mkOption {
          type = types.str;
          description = "Complete ordinary Caddy site blocks (without global settings).";
        };
      });
    };
    services = mkOption {
      default = { };
      description = "Systemd user services owned by hooddy.";
      type = types.attrsOf (types.submodule {
        options = {
          unit = mkOption {
            type = types.str;
            description = "Complete user unit with store executables and WantedBy=default.target.";
          };
          healthCheck = mkOption {
            default = null;
            description = "An optional loopback HTTP GET readiness check.";
            type = types.nullOr (types.submodule {
              options = {
                url = mkOption {
                  type = types.str;
                  description = "A loopback HTTP URL whose GET returns a 2xx status.";
                };
                timeoutSeconds = mkOption {
                  type = types.ints.positive;
                  description = "Maximum time to wait for readiness, in seconds.";
                };
              };
            });
          };
        };
      });
    };
  };
}
