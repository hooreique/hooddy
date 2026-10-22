# shellcheck shell=bash
set -euo pipefail

usage() {
  cat <<'EOF'
hooddy @version@

Usage: hooddy COMMAND [OPTIONS]
       hooddy --help
       hooddy --version

Commands:
  install [--http-port PORT] [--https-port PORT]
                             Install without enabling or starting the service;
                             enable user linger if needed. Fail if installed.
  uninstall [--disable-linger]
                             Stop, disable, and uninstall the service.
                             Preserve Caddy configuration and certificate data.
  start                      Enable and start the installed service.
  stop                       Disable and stop the installed service.
  restart                    Reload the running service, or start it if stopped.
                             Preserve its boot-time enablement setting.

All commands except install require an existing installation.
start, stop, and restart are idempotent.

Configuration: $XDG_CONFIG_HOME/hooddy (default: ~/.config/hooddy)
Site files:    sites.d/*.caddy, containing ordinary Caddyfile site blocks
Service:       hooddy.service in the current user's systemd manager

Port options apply only to install with a new Caddyfile (ports 1-65535).
Omitted ports use Caddy defaults. Existing Caddyfiles must be edited directly.

Run as the service's user. Linux, systemd user services, and Nix are required.
EOF
}

die() {
  echo "hooddy: $*" >&2
  exit 1
}

command_name=${1:---help}
if (( $# > 0 )); then
  shift
fi

case "$command_name" in
  --help|-h)
    (( $# == 0 )) || die "unexpected argument: $1"
    usage
    exit 0
    ;;
  --version|-V)
    (( $# == 0 )) || die "unexpected argument: $1"
    echo "hooddy @version@"
    exit 0
    ;;
  install|uninstall|start|stop|restart) ;;
  *) die "unknown command: $command_name (see hooddy --help)" ;;
esac

disable_linger=0
http_port=""
https_port=""
while (( $# > 0 )); do
  argument=$1
  shift
  case "$argument" in
    --help|-h)
      usage
      exit 0
      ;;
    --http-port|--https-port)
      [[ $command_name == install ]] || die "$argument is only valid with install"
      (( $# > 0 )) || die "$argument requires a port (1-65535)"
      port=$1
      shift
      [[ $port =~ ^[0-9]{1,5}$ ]] || die "$argument requires a port (1-65535)"
      port=$((10#$port))
      (( port >= 1 && port <= 65535 )) || die "$argument requires a port (1-65535)"
      if [[ $argument == --http-port ]]; then
        [[ -z $http_port ]] || die "--http-port specified more than once"
        http_port=$port
      else
        [[ -z $https_port ]] || die "--https-port specified more than once"
        https_port=$port
      fi
      ;;
    --disable-linger)
      [[ $command_name == uninstall ]] || die "--disable-linger is only valid with uninstall"
      disable_linger=1
      ;;
    *) die "unexpected argument: $argument (see hooddy --help)" ;;
  esac
done
if [[ -n $http_port && -n $https_port ]]; then
  [[ $http_port != "$https_port" ]] || die "HTTP and HTTPS ports must differ"
fi

[[ $OSTYPE == linux* ]] || die "only Linux with systemd user services is supported"
[[ -n ${HOME:-} ]] || die "HOME must be set"
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
for path in "$config_home" "$data_home"; do
  [[ $path == /* ]] || die "XDG paths must be absolute: $path"
  [[ $path != *[$'\n\r\t']* ]] || die "XDG paths must not contain control characters"
done
config_dir=$config_home/hooddy
caddyfile=$config_dir/Caddyfile
runtime_root=$config_dir/caddy
unit_dir=$config_home/systemd/user
unit_path=$unit_dir/hooddy.service
unit_name=hooddy.service

path_exists() {
  [[ -e $1 || -L $1 ]]
}

managed_unit() {
  [[ -f $unit_path ]] && [[ $(head -n 1 -- "$unit_path") == '# Managed by hooddy.' ]]
}

check_installation() {
  if [[ $command_name == install ]]; then
    if managed_unit; then
      die "already installed; uninstall before installing again"
    fi
    ! path_exists "$unit_path" || die "refusing to replace an existing unit: $unit_path"
    ! path_exists "$runtime_root" || die "installation is incomplete or conflicting: $runtime_root already exists"
  else
    path_exists "$unit_path" || die "not installed; run hooddy install first"
    managed_unit || die "unit is not managed by hooddy: $unit_path"
  fi
}

check_installation
systemctl --user show-environment >/dev/null 2>&1 || die "systemctl --user is unavailable; run in a working systemd user session"

# Serialize hooddy commands. Site writers only need ordinary, atomically replaced files.
mkdir -p -- "$config_dir"
exec 9>"$config_dir/.lock"
flock 9
check_installation

linger_status() {
  loginctl show-user "$(id -un)" --property=Linger --value || die "cannot query user linger with loginctl"
}

set_linger() {
  local desired=$1 status action
  status=$(linger_status) || exit 1
  [[ $status == yes || $status == no ]] || die "unexpected linger state: $status"
  [[ $status != "$desired" ]] || return 0
  action=enable-linger
  if [[ $desired == no ]]; then
    action=disable-linger
  fi
  loginctl "$action" "$(id -un)" || die "could not change linger; run loginctl $action $(id -un) with the required permissions"
  status=$(linger_status) || exit 1
  [[ $status == "$desired" ]] || die "linger did not change to $desired"
}

# systemd unit values use their own quoting and percent specifiers.
unit_string() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//%/%%}
  printf '"%s"' "$value"
}

unit_argument() {
  local value=$1
  value=${value//\$/\$\$}
  unit_string "$value"
}

install_service() {
  local existing_fragment temporary_unit="" created_root=0 created_unit=0
  existing_fragment=$(systemctl --user show "$unit_name" --property=FragmentPath --value) || die "cannot inspect $unit_name"
  [[ -z $existing_fragment ]] || die "a unit already exists at $existing_fragment"
  if path_exists "$caddyfile"; then
    [[ -f $caddyfile ]] || die "Caddyfile is not a regular file: $caddyfile"
    [[ -z $http_port && -z $https_port ]] || die "Caddyfile already exists; set http_port and https_port directly in $caddyfile"
  fi
  set_linger yes

  # Keep user configuration and linger on failure; undo only installation artifacts.
  # shellcheck disable=SC2329
  rollback_install() {
    local status=$?
    if (( status != 0 )); then
      if (( created_unit )); then
        rm -f -- "$unit_path"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
      fi
      if (( created_root )); then
        rm -f -- "$runtime_root"
      fi
      if [[ -n $temporary_unit ]]; then
        rm -f -- "$temporary_unit"
      fi
    fi
  }
  trap rollback_install EXIT

  mkdir -p -- "$config_dir/sites.d" "$unit_dir"
  if ! path_exists "$caddyfile"; then
    {
      # Caddy expands this environment placeholder when it loads the file.
      # shellcheck disable=SC2016
      printf '{\n  admin unix/{$XDG_RUNTIME_DIR}/hooddy/admin.sock\n'
      if [[ -n $http_port ]]; then
        printf '  http_port %s\n' "$http_port"
      fi
      if [[ -n $https_port ]]; then
        printf '  https_port %s\n' "$https_port"
      fi
      printf '}\n\nimport sites.d/*.caddy\n'
    } >"$caddyfile"
  fi

  created_root=1
  nix-store --add-root "$runtime_root" --indirect -r @caddy@ >/dev/null || die "could not retain Caddy in the Nix store"

  temporary_unit=$(mktemp "$unit_dir/.hooddy.service.XXXXXX")
  cat >"$temporary_unit" <<EOF
# Managed by hooddy.
[Unit]
Description=Hooddy shared Caddy server
After=network.target

[Service]
Type=notify
Environment=$(unit_string "XDG_CONFIG_HOME=$config_home")
Environment=$(unit_string "XDG_DATA_HOME=$data_home")
Environment="XDG_RUNTIME_DIR=%t"
RuntimeDirectory=hooddy
RuntimeDirectoryMode=0700
ExecStart=@caddy@/bin/caddy run --config $(unit_argument "$caddyfile") --adapter caddyfile
ExecReload=@caddy@/bin/caddy reload --config $(unit_argument "$caddyfile") --adapter caddyfile
Restart=on-failure
RestartSec=2s
TimeoutStopSec=30s

[Install]
WantedBy=default.target
EOF
  mv -T -- "$temporary_unit" "$unit_path"
  created_unit=1
  systemctl --user daemon-reload || die "could not load the installed unit"
  systemctl --user disable "$unit_name" || die "could not leave the installed unit disabled"
  trap - EXIT
  echo "hooddy: installed. Run hooddy start to enable and start the service."
}

case "$command_name" in
  install)
    install_service
    ;;
  uninstall)
    if path_exists "$runtime_root"; then
      [[ -L $runtime_root ]] || die "refusing to remove a non-symlink runtime: $runtime_root"
    fi
    systemctl --user disable --now "$unit_name" || die "could not disable and stop $unit_name"
    if (( disable_linger )); then
      set_linger no
    fi
    rm -f -- "$unit_path" "$runtime_root"
    systemctl --user daemon-reload || die "service files removed, but systemd daemon-reload failed"
    echo "hooddy: uninstalled. Configuration and certificate data were preserved."
    ;;
  start)
    set_linger yes
    systemctl --user enable --now "$unit_name" || die "could not enable and start $unit_name"
    ;;
  stop)
    systemctl --user disable --now "$unit_name" || die "could not disable and stop $unit_name"
    ;;
  restart)
    set_linger yes
    systemctl --user reload-or-restart "$unit_name" || die "could not reload or start $unit_name; see journalctl --user -u $unit_name"
    ;;
esac
