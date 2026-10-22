{
  description = "A shared Caddy service for user-space Linux applications";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      forAllSys =
        perSys:
        nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (
          system: perSys nixpkgs.legacyPackages.${system}
        );
    in
    {
      packages = forAllSys (pkgs: rec {
        hooddy = pkgs.callPackage ./package.nix { };
        default = hooddy;
      });

      checks = forAllSys (pkgs: {
        hooddy = pkgs.callPackage ./tests/package.nix {
          hooddy = self.packages.${pkgs.stdenv.hostPlatform.system}.hooddy;
        };
      });

      devShells = forAllSys (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.bashInteractive
            pkgs.caddy
            pkgs.python3
            pkgs.shellcheck
            pkgs.systemd
          ];
        };
      });
    };
}
