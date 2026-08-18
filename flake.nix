{
  description = "Pin revisions in west manifests to exact commit SHAs";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      overlays.default = final: prev: {
        pin-west = final.python3Packages.callPackage ./package.nix { };
      };

      packages = forAllSystems (pkgs: rec {
        pin-west = pkgs.python3Packages.callPackage ./package.nix { };
        default = pin-west;
      });

      apps = forAllSystems (pkgs: rec {
        pin-west = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.pin-west}/bin/pin-west";
        };
        default = pin-west;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python313
            pkgs.uv
            pkgs.git
          ];
          env.UV_PYTHON = "${pkgs.python313}/bin/python";
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
