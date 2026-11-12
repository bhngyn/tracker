"""Scaffold a new archive project folder, outside the repository.

usage: python new_project.py <project folder> --name "Display Name" --slug short_id --archive-root /path/to/archive

Creates <folder>/project.toml (from project.example.toml, with the given values filled in)
and an empty <folder>/watchlist.json. Refuses to write inside this git repository, because
project folders name research targets and must never be committed.
"""
import argparse, re, subprocess, sys
from pathlib import Path

TOOLKIT = Path(__file__).resolve().parent


def repo_root():
    r = subprocess.run(["git", "-C", str(TOOLKIT), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--name", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--archive-root", required=True)
    ap.add_argument("--inside-repo-ok", action="store_true",
                    help="allow a folder inside the repo (only if it is git-ignored/excluded)")
    a = ap.parse_args()

    dest = Path(a.folder).expanduser().resolve()
    repo = repo_root()
    if repo and (dest == repo or repo in dest.parents) and not a.inside_repo_ok:
        sys.exit(f"{dest} is inside the git repository {repo}. Put project folders elsewhere "
                 f"(or pass --inside-repo-ok after excluding it in .git/info/exclude).")
    if not re.fullmatch(r"[a-z0-9_-]+", a.slug):
        sys.exit("--slug: lowercase letters, digits, - and _ only")
    cfg = dest / "project.toml"
    if cfg.exists():
        sys.exit(f"{cfg} already exists")
    dest.mkdir(parents=True, exist_ok=True)
    t = (TOOLKIT / "project.example.toml").read_text(encoding="utf-8")
    t = t.replace('name = "Example Organisation"', f'name = "{a.name}"', 1)
    t = t.replace('slug = "example"', f'slug = "{a.slug}"', 1)
    t = t.replace('archive_root = "/Volumes/Drive/example_archive"', f'archive_root = "{Path(a.archive_root).expanduser()}"', 1)
    t = t.replace('path = ""                                     # JSON list', 'path = "watchlist.json"                       # JSON list', 1)
    cfg.write_text(t, encoding="utf-8")
    (dest / "watchlist.json").write_text("[]\n", encoding="utf-8")
    print(f"created {cfg}\n        {dest / 'watchlist.json'}\n\nnext: edit the config (targets, budgets, languages), fill the watchlist, then\n"
          f"  ARCHIVE_CONFIG={cfg} {TOOLKIT / '.venv/bin/python'} {TOOLKIT / 'config.py'} check\n"
          f"  nohup {TOOLKIT / 'run_all.sh'} --config {cfg} >/dev/null 2>&1 &")


if __name__ == "__main__":
    main()
