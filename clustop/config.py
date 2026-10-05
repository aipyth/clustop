"""Profiles: named sets of hosts, stored in ~/.config/clustop/config.toml.

    default = "gpu-lab"

    [profiles.gpu-lab]
    hosts = ["10.0.0.1", "10.0.0.2"]
    user = "me"          # optional, ssh user (otherwise ~/.ssh/config decides)
    interval = 2.0       # optional, refresh seconds
"""
import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CLUSTOP_CONFIG")
                   or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clustop" / "config.toml")

SEED = {"default": "", "profiles": {}}


def _key(name):
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def dumps(cfg) -> str:
    out = ["# clustop config. Edit by hand or use `clustop profile ...`", ""]
    if cfg.get("default"):
        out += [f"default = {json.dumps(cfg['default'])}", ""]
    for name, p in cfg.get("profiles", {}).items():
        out.append(f"[profiles.{_key(name)}]")
        out.append(f"hosts = {json.dumps(p['hosts'])}")
        if p.get("user"):
            out.append(f"user = {json.dumps(p['user'])}")
        if p.get("interval"):
            out.append(f"interval = {float(p['interval'])}")
        out.append("")
    return "\n".join(out)


def save(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(dumps(cfg))


def load() -> dict:
    """Load the config, creating an empty one on first run."""
    if not CONFIG_PATH.exists():
        save(SEED)
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"clustop: cannot parse {CONFIG_PATH}: {e}")
    cfg.setdefault("profiles", {})
    for n, p in cfg["profiles"].items():
        if not p.get("hosts"):
            sys.exit(f"clustop: profile '{n}' in {CONFIG_PATH} has no hosts")
    return cfg


def pick(cfg, name=None):
    """Return (profile_name, profile) for an explicit name, else the default."""
    profiles = cfg["profiles"]
    if not profiles:
        sys.exit("clustop: no profiles yet. Create one with:\n"
                 "  clustop profile add NAME HOST [HOST...] --user YOUR_USER --default\n"
                 "or try it right away:  clustop HOST [HOST...]")
    if name is None:
        name = cfg.get("default") or next(iter(profiles), None)
    if name not in profiles:
        known = ", ".join(profiles) or "none"
        sys.exit(f"clustop: no profile '{name}' (available: {known}). "
                 f"Create one with: clustop profile add {name} HOST [HOST...]")
    return name, profiles[name]


def profile_cli(argv):
    ap = argparse.ArgumentParser(prog="clustop profile", description="Manage cluster profiles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list profiles (* = default)")
    s = sub.add_parser("show", help="show one profile"); s.add_argument("name", nargs="?")
    s = sub.add_parser("add", help="create or replace a profile")
    s.add_argument("name"); s.add_argument("hosts", nargs="+")
    s.add_argument("-u", "--user", help="ssh user for all hosts")
    s.add_argument("-i", "--interval", type=float, help="default refresh seconds")
    s.add_argument("--default", action="store_true", help="also make it the default profile")
    s = sub.add_parser("remove", help="delete a profile"); s.add_argument("name")
    s = sub.add_parser("default", help="make a profile the default"); s.add_argument("name")
    sub.add_parser("path", help="print the config file location")
    a = ap.parse_args(argv)

    cfg = load()
    profs = cfg["profiles"]
    if a.cmd == "path":
        print(CONFIG_PATH)
    elif a.cmd == "list":
        for n, p in profs.items():
            print(f"{'*' if n == cfg.get('default') else ' '} {n:<14} {len(p['hosts'])} hosts: {' '.join(p['hosts'])}")
        if not profs:
            print("no profiles yet: clustop profile add NAME HOST [HOST...]")
    elif a.cmd == "show":
        n, p = pick(cfg, a.name)
        print(dumps({"default": cfg.get("default") if n == cfg.get("default") else None, "profiles": {n: p}}))
    elif a.cmd == "add":
        p = {"hosts": a.hosts}
        if a.user: p["user"] = a.user
        if a.interval: p["interval"] = a.interval
        profs[a.name] = p
        if a.default or not cfg.get("default"):
            cfg["default"] = a.name
        save(cfg)
        print(f"saved profile '{a.name}'" + (" (default)" if cfg["default"] == a.name else ""))
    elif a.cmd == "remove":
        pick(cfg, a.name)
        del profs[a.name]
        if cfg.get("default") == a.name:
            cfg["default"] = next(iter(profs), "")
        save(cfg)
        print(f"removed '{a.name}'" + (f"; default is now '{cfg['default']}'" if cfg["default"] else ""))
    elif a.cmd == "default":
        pick(cfg, a.name)
        cfg["default"] = a.name
        save(cfg)
        print(f"default profile is now '{a.name}'")
