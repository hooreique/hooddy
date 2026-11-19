{ nixpkgs }:
let
  inherit (nixpkgs) lib;
  supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
  fail = message: throw "hooddy: ${message}";
  require = condition: message: value: if condition then value else fail message;
  nameValid = name: builtins.match "[a-zA-Z0-9][a-zA-Z0-9_-]*" name != null;
  lines = lib.splitString "\n";
  trim = lib.strings.trim;
  # Only literal addresses are inspected. Caddy remains authoritative for
  # placeholders, snippets, imported text, and the complete user configuration.
  literalAddresses = text:
    let
      step = state: line:
        let
          clean = trim (builtins.head (lib.splitString "#" line));
          header = builtins.match "([^{}]+)[[:space:]]*\\{.*" clean;
          opens = builtins.length (lib.splitString "{" clean) - 1;
          closes = builtins.length (lib.splitString "}" clean) - 1;
          words = map (word: lib.removeSuffix "\"" (lib.removePrefix "\"" word)) (lib.filter (word: word != "") (lib.splitString " " (lib.replaceStrings [ "," "\t" ] [ " " " " ] (if header == null then "" else trim (builtins.head header)))));
          literal = word: builtins.match "(https?://)?[a-zA-Z0-9.*_-]+(:[0-9]+)?" word != null;
          normalize = lib.toLower;
        in
        {
          depth = state.depth + opens - closes;
          addresses = state.addresses ++ lib.optionals (state.depth == 0 && header != null) (map normalize (lib.filter literal words));
        };
    in
    (lib.foldl' step { depth = 0; addresses = [ ]; } (lines text)).addresses;
  duplicates = values: lib.unique (lib.filter (value: builtins.length (lib.filter (other: value == other) values) > 1) values);
  healthParts = url: builtins.match "http://(localhost|127\\.([0-9]{1,3})\\.([0-9]{1,3})\\.([0-9]{1,3})|[[]::1[]])(:([0-9]{1,5}))?([/?][^[:space:]#]*)?" url;
  validHealth = url:
    let parts = healthParts url; in
    parts != null
    && lib.all (part: part == null || lib.toIntBase10 part <= 255) (map (builtins.elemAt parts) [ 1 2 3 ])
    && (builtins.elemAt parts 5 == null || (let port = lib.toIntBase10 (builtins.elemAt parts 5); in port > 0 && port <= 65535));
  unitValid = unit:
    let
      logical = lib.foldl' (state: raw:
        let line = trim raw; joined = state.pending + line; in
        if lib.hasPrefix "#" line || lib.hasPrefix ";" line then state
        else if lib.hasSuffix "\\" joined then state // { pending = lib.removeSuffix "\\" joined + " "; }
        else { pending = ""; entries = state.entries ++ [ joined ]; }
      ) { pending = ""; entries = [ ]; } (lines unit);
      absoluteExec = value: builtins.match "[@:+!-]*\"?/nix/store/[a-z0-9]{32}-[^[:space:]\"]+.*" value != null;
      parsed = lib.foldl' (state: line:
        let
          section = builtins.match "[[]([^]]+)[]]" line;
          directive = builtins.match "([A-Za-z][A-Za-z0-9]*)[[:space:]]*=[[:space:]]*(.*)" line;
          key = if directive == null then "" else builtins.head directive;
          value = if directive == null then "" else trim (builtins.elemAt directive 1);
          execution = builtins.elem key [ "ExecStart" "ExecStartPre" "ExecStartPost" "ExecReload" "ExecStop" "ExecStopPost" "ExecCondition" ];
        in
        if section != null then state // { section = builtins.head section; }
        else state // {
          safe = state.safe
            && (!execution || value == "" || absoluteExec value)
            && (state.section != "Install" || line == "" || (key == "WantedBy" && builtins.elem value [ "" "default.target" ]));
          start = if state.section == "Service" && key == "ExecStart" then value != "" else state.start;
          wanted = if state.section == "Install" && key == "WantedBy" then value == "default.target" else state.wanted;
        }
      ) { section = ""; safe = true; start = false; wanted = false; } logical.entries;
    in logical.pending == "" && parsed.safe && parsed.start && parsed.wanted;
in
{
  hooddyConfiguration = { system, modules, specialArgs ? { } }:
    let
      reserved = lib.intersectLists [ "lib" "config" "options" "pkgs" ] (builtins.attrNames specialArgs);
      pkgs = nixpkgs.legacyPackages.${system};
      evaluated = lib.evalModules {
        class = "hooddy";
        modules = [ ../modules/default.nix ] ++ modules;
        specialArgs = specialArgs // { inherit pkgs; };
      };
      inherit (evaluated) config options;
      inherit (config.hooddy) enable sites services httpPort httpsPort;
      names = builtins.attrNames sites ++ builtins.attrNames services;
      siteAddresses = lib.concatMap (name: literalAddresses sites.${name}.config) (builtins.attrNames sites);
      # Public application options remain application-owned. These common
      # literal fields let independent modules catch obvious backend conflicts.
      applications = lib.filter (app: builtins.isAttrs app && (app.enable or false) == true) (builtins.attrValues (config.services or { }));
      ports = map (app: app.port) (lib.filter (app: app ? port && builtins.isInt app.port) applications);
      domains = map (app: lib.toLower app.domain) (lib.filter (app: app ? domain && builtins.isString app.domain) applications);
      assertions = [
        { ok = lib.all nameValid names; message = "site and service names must match [a-zA-Z0-9][a-zA-Z0-9_-]*"; }
        { ok = lib.all (name: unitValid services.${name}.unit) (builtins.attrNames services); message = "units need [Service] ExecStart with absolute /nix/store executables and only [Install] WantedBy=default.target"; }
        { ok = lib.all (name: services.${name}.healthCheck == null || validHealth services.${name}.healthCheck.url) (builtins.attrNames services); message = "healthCheck.url must be a loopback HTTP URL with a valid port"; }
        { ok = duplicates ports == [ ]; message = "duplicate application ports: ${lib.concatMapStringsSep ", " toString (duplicates ports)}"; }
        { ok = duplicates domains == [ ]; message = "duplicate application domains: ${lib.concatStringsSep ", " (duplicates domains)}"; }
        { ok = duplicates siteAddresses == [ ]; message = "duplicate literal Caddy site addresses: ${lib.concatStringsSep ", " (duplicates siteAddresses)}"; }
      ];
      validate = value: lib.foldr (assertion: next: require assertion.ok assertion.message next) value (lib.optionals enable assertions);
      siteText = lib.concatMapStrings (name: sites.${name}.config + "\n") (builtins.attrNames sites);
      caddy = lib.getExe pkgs.caddy;
      globalOptions = [ "\tadmin unix/{$XDG_RUNTIME_DIR}/hooddy/admin.sock" ]
        ++ lib.optional (httpPort != null) "\thttp_port ${toString httpPort}"
        ++ lib.optional (httpsPort != null) "\thttps_port ${toString httpsPort}";
      caddyfile = pkgs.writeText "hooddy-Caddyfile" ''
        {
        ${lib.concatStringsSep "\n" globalOptions}
        }

        ${siteText}
      '';
      caddyUnit = pkgs.writeText "hooddy.service" ''
        [Unit]
        Description=Hooddy shared Caddy server
        After=network.target

        [Service]
        Type=notify
        RuntimeDirectory=hooddy
        RuntimeDirectoryMode=0700
        ExecStart=${caddy} run --config ${caddyfile} --adapter caddyfile
        ExecReload=${caddy} reload --config ${caddyfile} --adapter caddyfile
        Restart=on-failure
        RestartSec=2s
        TimeoutStopSec=30s

        [Install]
        WantedBy=default.target
      '';
      manifest = {
        protocolVersion = 2;
        inherit system;
        enabled = enable;
        caddy = if enable then caddy else null;
        services = lib.optionalAttrs enable (lib.mapAttrs (name: service: {
          unit = "units/hooddy-${name}.service";
          inherit (service) healthCheck;
        }) services);
      };
      activationPackage = pkgs.runCommand "hooddy-generation" { } ''
        mkdir -p "$out"
        cp ${pkgs.writeText "hooddy-manifest.json" (builtins.toJSON manifest)} "$out/manifest.json"
        ${lib.optionalString enable ''
          mkdir -p "$out/units"
          cp ${caddyfile} "$out/Caddyfile"
          cp ${caddyUnit} "$out/units/hooddy.service"
          ${lib.concatMapStringsSep "\n" (name: ''
            cp ${pkgs.writeText "hooddy-${name}.service" services.${name}.unit} "$out/units/hooddy-${name}.service"
          '') (builtins.attrNames services)}
        ''}
      '';
    in
    require (builtins.elem system supportedSystems) "unsupported system ${system}; expected x86_64-linux or aarch64-linux" (
      require (reserved == [ ]) "specialArgs cannot override reserved arguments: ${lib.concatStringsSep ", " reserved}" (
        validate { inherit config options activationPackage; }
      )
    );
}
