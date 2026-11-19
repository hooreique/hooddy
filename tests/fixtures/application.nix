# A system-independent application module, mirroring the two external apps.
{ name, port, staticClient ? false }:
{ lib, config, pkgs, ... }:
let
  cfg = config.services.${name};
  supported = [ "x86_64-linux" "aarch64-linux" ];
  package = if builtins.elem pkgs.stdenv.hostPlatform.system supported then
    pkgs.writeShellScriptBin name ''
      exec ${pkgs.python3}/bin/python3 -m http.server "$@"
    ''
  else throw "${name}: unsupported system ${pkgs.stdenv.hostPlatform.system}";
  client = pkgs.writeTextDir "index.html" "${name} fixture client";
  unitQuote = value: "\"" + lib.replaceStrings [ "\\" "\"" "%" "$" ] [ "\\\\" "\\\"" "%%" "$$" ] value + "\"";
  caddyQuote = value: "\"" + lib.replaceStrings [ "\\" "\"" ] [ "\\\\" "\\\"" ] value + "\"";
in
{
  options.services.${name} = {
    enable = lib.mkEnableOption name;
    domain = lib.mkOption { type = lib.types.str; default = "${name}.example.test"; };
    host = lib.mkOption { type = lib.types.str; default = "127.0.0.1"; };
    port = lib.mkOption { type = lib.types.port; default = port; };
  };
  config = lib.mkIf cfg.enable {
    hooddy.services.${name} = {
      unit = ''
        [Unit]
        Description=${name} fixture
        After=network.target
        [Service]
        ${lib.optionalString (!staticClient) "EnvironmentFile=-%h/.config/${name}/environment"}
        ExecStart=${package}/bin/${name} ${unitQuote (toString cfg.port)} --bind ${unitQuote cfg.host}
        Restart=on-failure
        [Install]
        WantedBy=default.target
      '';
      healthCheck = { url = "http://${cfg.host}:${toString cfg.port}/"; timeoutSeconds = 5; };
    };
    hooddy.sites.${name}.config = ''
      ${caddyQuote cfg.domain} {
        ${if staticClient then ''
          handle /api/* {
            reverse_proxy ${caddyQuote "${cfg.host}:${toString cfg.port}"}
          }
          handle {
            root * ${caddyQuote (toString client)}
            file_server
          }
        '' else ''
          reverse_proxy ${caddyQuote "${cfg.host}:${toString cfg.port}"}
        ''}
      }
    '';
  };
}
