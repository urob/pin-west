{
  lib,
  buildPythonApplication,
  uv-build,
  west,
  packaging,
  pyyaml,
  pytestCheckHook,
  git,
}:

let
  inherit (lib.importTOML ./pyproject.toml) project;
in
buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      ./LICENSE # declared by pyproject's license-files; uv_build requires it
      ./src
      ./tests
    ];
  };

  # nixpkgs ships uv-build 0.11, the lockfile pins 0.12; the backend's
  # src-layout discovery is unchanged between them.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail '"uv_build>=0.12.5,<0.13.0"' '"uv_build"'
  '';

  build-system = [ uv-build ];

  dependencies = [
    west
    packaging
    pyyaml
  ];

  nativeCheckInputs = [
    pytestCheckHook
    git
    west # the workspace tests shell out to the `west` CLI
  ];

  # The suite builds real local git repos as stand-in remotes.
  preCheck = ''
    export HOME=$(mktemp -d)
    git config --global user.email pin-west@example.com
    git config --global user.name pin-west
    git config --global init.defaultBranch main
  '';

  pythonImportsCheck = [ "pin_west" ];

  meta = {
    inherit (project) description;
    homepage = project.urls.Homepage;
    license = lib.getLicenseFromSpdxId project.license;
    mainProgram = lib.head (lib.attrNames project.scripts);
  };
}
