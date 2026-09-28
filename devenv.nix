{pkgs, ...}: let
  python = pkgs.python3.withPackages (ps: [ps.click ps.python-gitlab ps.pytest ps.ruff ps.hatchling ps.build]);
in {
  packages = [python pkgs.git pkgs.committed pkgs.actionlint pkgs.alejandra];
  git-hooks.hooks.committed = {
    enable = true;
    name = "Conventional commit message";
    entry = "${pkgs.committed}/bin/committed --config committed.toml --commit-file";
    stages = ["commit-msg"];
  };
  tasks = {
    "quality:lint" = {
      exec = "ruff check . && ruff format --check . && actionlint && alejandra --check devenv.nix";
      showOutput = true;
    };
    "test:all" = {
      exec = "pytest && python -m build --no-isolation";
      after = ["quality:lint"];
      showOutput = true;
    };
  };
  enterTest = "devenv tasks run test:all";
}
