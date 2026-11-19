# shellcheck shell=bash
set -euo pipefail

usage() {
  cat <<'HELP'
hooddy @version@

Usage: hooddy switch
       hooddy --help
       hooddy --version

Apply ~/.config/hooddy/flake.nix to the current user's systemd manager.
Set hooddy.enable = false in the configuration and switch to remove the services.
HELP
}

die() {
  echo "hooddy: $*" >&2
  exit 1
}

case ${1---help} in
  --help|-h)
    (( $# <= 1 )) || die "unexpected argument: $2"
    usage
    exit 0
    ;;
  --version|-V)
    (( $# == 1 )) || die "unexpected argument: $2"
    echo "hooddy @version@"
    exit 0
    ;;
  switch) shift ;;
  *) die "unknown command: $1 (see hooddy --help)" ;;
esac
if (( $# == 1 )) && [[ $1 == --help || $1 == -h ]]; then
  usage
  exit 0
fi
(( $# == 0 )) || die "unexpected argument: $1"

[[ ${HOME:-} == /* ]] || die "HOME must be an absolute path"
[[ ${XDG_CONFIG_HOME:-$HOME/.config} == /* ]] || die "XDG_CONFIG_HOME must be an absolute path"
config_dir=$HOME/.config/hooddy
[[ -d $config_dir ]] || die "missing $config_dir/flake.nix"
exec @python@ @switch@
