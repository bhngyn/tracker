"""Project configuration: every target-, budget- and wording-specific setting lives in a
project TOML file outside this toolkit (see project.example.toml).

The config is found through the ARCHIVE_CONFIG environment variable (run_all.sh sets it
from its --config argument, and every child process inherits it). Relative paths in the
file resolve against the file's own folder.

CLI (used by the shell scripts):
    python config.py get youtube.tabs          -> one value per line (lists), JSON (tables)
    python config.py get project.slug
    python config.py check                     -> validate and print a summary
"""
import json, os, sys, tomllib
from pathlib import Path

TOOLKIT = Path(__file__).resolve().parent

DEFAULTS = {
    "project": {"name": "Archive", "slug": "archive"},
    "apify": {"enabled": False, "token_env": "APIFY_API_TOKEN", "env_file": "", "total_budget_usd": 0.0,
              "untracked_spend_usd": 0.0},
    "youtube": {"tabs": [], "sub_langs": "en.*,-live_chat", "caption_langs": ["en"], "max_res": 720,
                "expected_videos": 0},
    "instagram": {"accounts": [], "dataset": "instagram", "actor": "apify~instagram-post-scraper",
                  "results_limit": 5000, "budget_usd": 0.0, "profile_posts": {}, "profile_posts_date": ""},
    "facebook": {"pages": [], "actor": "apify~facebook-posts-scraper"},
    "transcribe": {"model": "mlx-community/whisper-large-v3-turbo", "youtube": "missing",
                   "temperatures": [0.0, 0.4, 0.8], "max_rss_gb": 5.0},
    "ocr": {"languages": ["en-US"], "interval_s": 5.0, "workers": 1, "max_rss_gb": 1.5},
    "processing": {"mem_hold_pct": 5, "mem_resume_pct": 10},
    "watchlist": {"path": "", "label": "Places", "native_prefixes": "", "native_letters": ""},
    "viewer": {"title": "", "search_placeholder": "Search everything — names, places, phrases"},
    "methodology": {"purpose": "", "watchlist_description": "", "extra_limitations": []},
}


class ConfigError(SystemExit):
    pass


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(path: str | os.PathLike | None = None) -> dict:
    path = path or os.environ.get("ARCHIVE_CONFIG")
    if not path:
        raise ConfigError("No project config. Set ARCHIVE_CONFIG=/path/to/project.toml "
                          "(or pass --config to run_all.sh). See project.example.toml.")
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ConfigError(f"Project config not found: {p}")
    cfg = _merge(DEFAULTS, tomllib.loads(p.read_text(encoding="utf-8")))
    cfg["_path"] = str(p)
    cfg["_dir"] = str(p.parent)
    pr = cfg["project"]
    if not pr.get("archive_root"):
        raise ConfigError(f"{p}: [project] archive_root is required")
    pr["archive_root"] = str(resolve(cfg, pr["archive_root"]))
    pr["packages_dir"] = str(resolve(cfg, pr.get("packages_dir") or Path(pr["archive_root"]).parent / f"{pr['slug']}_packages"))
    cfg["viewer"]["title"] = cfg["viewer"]["title"] or pr["name"]
    for i, page in enumerate(cfg["facebook"]["pages"]):
        page.setdefault("dataset", "facebook_history" if i == 0 else f"facebook_history_{i + 1}")
        page.setdefault("budget_usd", 0.0)
        page.setdefault("older_than", "")
    return cfg


def resolve(cfg: dict, value) -> Path:
    """Paths in the config are relative to the config file's folder."""
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (Path(cfg["_dir"]) / p).resolve()


def get(cfg: dict, dotted: str, default=None):
    cur = cfg
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def _main(argv):
    if len(argv) >= 2 and argv[0] == "get":
        v = get(load(), argv[1])
        if v is None:
            sys.exit(1)
        if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
            print("\n".join(str(x) for x in v))
        elif isinstance(v, (dict, list)):
            print(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, bool):
            print("true" if v else "false")
        else:
            print(v)
    elif argv[:1] == ["check"]:
        c = load()
        pr = c["project"]
        print(f"project   {pr['name']} ({pr['slug']})\narchive   {pr['archive_root']}\npackages  {pr['packages_dir']}")
        print(f"youtube   {len(c['youtube']['tabs'])} tab(s)\ninstagram {len(c['instagram']['accounts'])} account(s)\n"
              f"facebook  {len(c['facebook']['pages'])} page(s)\napify     {'enabled' if c['apify']['enabled'] else 'disabled'}, "
              f"budget ${c['apify']['total_budget_usd']:.2f}")
        wl = c["watchlist"]["path"]
        print(f"watchlist {resolve(c, wl) if wl else '(none: place matching off)'}")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    _main(sys.argv[1:])
