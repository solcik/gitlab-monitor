{pkgs, ...}: let
  python = pkgs.python3.withPackages (ps: [ps.click ps.python-gitlab ps.pytest ps.ruff ps.hatchling]);
in {
  packages = [python pkgs.git pkgs.committed];
  git-hooks.hooks.committed = {
    enable = true;
    name = "Conventional commit message";
    entry = "${pkgs.committed}/bin/committed --config committed.toml --commit-file";
    stages = ["commit-msg"];
  };
  tasks = {
    "quality:lint".exec = "ruff check . && ruff format --check .";
    "test:all".exec = "pytest";
  };
}
