"""clustop: live CPU / RAM / GPU overview of several hosts over SSH.

Agentless: each poll runs one shell snippet on the host (reads /proc and calls
nvidia-smi) through a persistent SSH ControlMaster connection, so you type a
password at most once per host (the connection stays alive for 8h).
"""
import argparse
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field

from . import config
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PROFILE = ""  # name of the active profile, shown in the header
CTL_PATH = os.path.expanduser("~/.ssh/clustop-%C")
SSH_BASE = ["ssh", "-o", f"ControlPath={CTL_PATH}", "-o", "ConnectTimeout=6"]

REMOTE = r"""
echo '##HOST'; hostname -s
echo '##CPU'; head -1 /proc/stat
echo '##NCPU'; nproc
echo '##LOAD'; cat /proc/loadavg
echo '##MEM'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo '##GPU'
G=$(timeout 8 nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits 2>/dev/null); RC=$?
echo "$G"
echo '##APPS'
if [ $RC -eq 0 ]; then
  A=$(timeout 5 nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv,noheader,nounits 2>/dev/null)
  echo "$A"
  P=$(echo "$A" | cut -d, -f2 | tr -d ' ' | grep -E '^[0-9]+$' | sort -u | paste -sd, -)
fi
echo '##PS'
[ -n "$P" ] && ps -o pid=,user:24=,etime=,args= -p "$P" 2>/dev/null
echo '##CG'
for p in $(echo "$P" | tr ',' ' '); do j=$(grep -o 'job_[0-9]*' /proc/$p/cgroup 2>/dev/null | head -1); echo "$p ${j#job_}"; done
echo '##GPUERR'; [ $RC -eq 0 ] || echo "nvidia-smi failed/hung (rc=$RC)"
exit 0
"""


@dataclass
class Gpu:
    idx: int
    uuid: str
    name: str
    util: float
    mem_used: float
    mem_total: float
    temp: float
    power: float
    power_limit: float


@dataclass
class Proc:
    gpu: int
    pid: int
    user: str
    mem: float
    etime: str
    cmd: str
    job: str = ""


@dataclass
class HostState:
    addr: str
    name: str = ""
    ok: bool = False
    error: str = "connecting..."
    updated: float = 0.0
    cpu_pct: float = 0.0
    ncpu: int = 0
    load: tuple = (0.0, 0.0, 0.0)
    mem_total: float = 0.0
    mem_used: float = 0.0
    gpus: list = field(default_factory=list)
    procs: list = field(default_factory=list)
    gpu_err: str = ""
    target: str = ""  # what ssh connects to (user@addr)
    _prev_cpu: tuple = None


def num(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse(host: HostState, out: str):
    sec, data = None, {}
    for line in out.splitlines():
        if line.startswith("##"):
            sec = line[2:].strip()
            data[sec] = []
        elif sec:
            data[sec].append(line)

    host.name = (data.get("HOST") or [host.addr])[0].strip()
    host.ncpu = int((data.get("NCPU") or ["0"])[0] or 0)
    la = (data.get("LOAD") or ["0 0 0"])[0].split()
    host.load = tuple(num(x) for x in la[:3])

    # CPU: delta of /proc/stat between two polls
    f = [int(x) for x in (data.get("CPU") or [""])[0].split()[1:]]
    if f:
        idle, total = f[3] + (f[4] if len(f) > 4 else 0), sum(f[:8])
        if host._prev_cpu:
            di, dt = idle - host._prev_cpu[0], total - host._prev_cpu[1]
            if dt > 0:
                host.cpu_pct = 100.0 * (1 - di / dt)
        host._prev_cpu = (idle, total)

    mem = {}
    for l in data.get("MEM", []):
        k, v = l.split(":")
        mem[k] = int(v.split()[0]) / 1024  # MiB
    host.mem_total = mem.get("MemTotal", 0)
    host.mem_used = host.mem_total - mem.get("MemAvailable", 0)

    host.gpus = []
    for l in data.get("GPU", []):
        p = [x.strip() for x in l.split(",")]
        if len(p) >= 9:
            host.gpus.append(Gpu(int(p[0]), p[1], p[2], num(p[3]), num(p[4]), num(p[5]),
                                 num(p[6]), num(p[7]), num(p[8])))
    uuid2idx = {g.uuid: g.idx for g in host.gpus}

    ps = {}
    for l in data.get("PS", []):
        p = l.split(None, 3)
        if len(p) >= 3:
            ps[int(p[0])] = (p[1], p[2], p[3] if len(p) > 3 else "")
    cg = {}
    for l in data.get("CG", []):
        p = l.split()
        if p and p[0].isdigit():
            cg[int(p[0])] = p[1] if len(p) > 1 else ""
    host.procs = []
    for l in data.get("APPS", []):
        p = [x.strip() for x in l.split(",")]
        if len(p) >= 4 and p[1].isdigit():
            pid = int(p[1])
            user, et, cmd = ps.get(pid, ("?", "?", p[3]))
            host.procs.append(Proc(uuid2idx.get(p[0], -1), pid, user, num(p[2]), et, cmd, cg.get(pid, "")))
    host.ok, host.error, host.updated = True, "", time.time()
    host.gpu_err = " ".join(data.get("GPUERR", [])).strip()


def poll(host: HostState):
    try:
        r = subprocess.run(
            SSH_BASE + ["-o", "BatchMode=yes", host.target or host.addr, "bash -s"],
            input=REMOTE, capture_output=True, text=True, timeout=30)
        if r.returncode != 0 and not r.stdout:
            host.ok, host.error = False, (r.stderr.strip().splitlines() or ["ssh failed"])[-1]
        else:
            parse(host, r.stdout)
    except Exception as e:  # timeout etc.
        host.ok = False
        host.error = ("timed out after 30s (host unresponsive or hung)" if isinstance(e, subprocess.TimeoutExpired)
                      else f"{type(e).__name__}: {e}")


def connect(addr: str) -> bool:
    """Open (or reuse) the persistent master connection. May prompt for a password."""
    chk = subprocess.run(SSH_BASE + ["-O", "check", addr], capture_output=True)
    if chk.returncode == 0:
        return True
    print(f"Connecting to {addr} ...")
    r = subprocess.run(SSH_BASE + ["-o", "ControlMaster=auto", "-o", "ControlPersist=8h",
                                   "-o", "ServerAliveInterval=30", addr, "true"])
    return r.returncode == 0


# ---------------------------------------------------------------- rendering

def color(p, warn=60, crit=85):
    return "green" if p < warn else "yellow" if p < crit else "red"


def bar(pct, width, label="", warn=60, crit=85):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    t = Text()
    t.append("█" * filled, style=color(pct, warn, crit))
    t.append("░" * (width - filled), style="grey35")
    if label:
        t.append(" " + label)
    return t


def short_cmd(cmd):
    toks = cmd.split()
    out = []
    for i, t in enumerate(toks):
        if "/" in t and not t.startswith("-"):
            t = t.rstrip("/").rsplit("/", 1)[-1] if i == 0 else ".../" + t.rstrip("/").rsplit("/", 1)[-1]
        out.append(t)
    return " ".join(out)


def gib(mib):
    return f"{mib / 1024:.1f}G" if mib >= 1024 else f"{mib:.0f}M"


def gpu_state(gpu, procs):
    """Returns (kind, Text) where kind is free | unknown | used."""
    mine = [p for p in procs if p.gpu == gpu.idx]
    if not mine:
        if gpu.util < 5 and gpu.mem_used < 1000:
            return "free", Text(" FREE ", style="bold black on green")
        return "unknown", Text("busy, proc not visible", style="yellow")
    t = Text(f"{len(mine)}p ", style="bold")
    per = {}
    for p in mine:
        per.setdefault(p.user, set()).add("S" if p.job else "M")
    for i, (u, kinds) in enumerate(sorted(per.items())):
        if i:
            t.append(" ")
        t.append(u, style="magenta")
        t.append(":" + "".join(sorted(kinds)), style="cyan" if kinds == {"S"} else "yellow")
    return "used", t


def host_panel(h: HostState, bw: int) -> Panel:
    title = Text()
    title.append(h.name or h.addr, style="bold cyan")
    title.append(f"  {h.addr}", style="dim")
    if not h.ok:
        return Panel(Text(f"✗ {h.error}", style="red"), title=title, border_style="red")

    nfree = sum(gpu_state(g, h.procs)[0] == "free" for g in h.gpus)
    if h.gpus:
        title.append(f"   {nfree}/{len(h.gpus)} GPU free", style="bold green" if nfree else "dim")
    stale = time.time() - h.updated > 10
    g = Table.grid(padding=(0, 1))
    g.add_column(no_wrap=True)
    mpct = 100 * h.mem_used / h.mem_total if h.mem_total else 0
    cpu = Text("CPU ") + bar(h.cpu_pct, bw, f"{h.cpu_pct:4.0f}%  ld {h.load[0]:.1f}/{h.ncpu}c")
    ram = Text("RAM ") + bar(mpct, bw, f"{mpct:4.0f}%  {gib(h.mem_used)}/{gib(h.mem_total)}", 70, 90)
    g.add_row(cpu)
    g.add_row(ram)

    gt = Table(box=None, padding=(0, 1), show_header=True, header_style="dim", expand=False)
    for c in ("GPU", "Name", "Util", "Memory", "T", "Who  (S=slurm M=manual)"):
        gt.add_column(c, no_wrap=True)
    for gpu in h.gpus:
        mp = 100 * gpu.mem_used / gpu.mem_total if gpu.mem_total else 0
        gt.add_row(
            Text(str(gpu.idx), style="bold green" if gpu_state(gpu, h.procs)[0] == "free" else ""), gpu.name.replace("NVIDIA ", "")[:22],
            bar(gpu.util, 10, f"{gpu.util:3.0f}%"),
            bar(mp, 10, f"{gib(gpu.mem_used)}/{gib(gpu.mem_total)}", 70, 90),
            Text(f"{gpu.temp:.0f}°", style=color(gpu.temp, 65, 80)),
            gpu_state(gpu, h.procs)[1],
        )
    if not h.gpus:
        gt = Text(h.gpu_err or "no GPUs found", style="red" if h.gpu_err else "dim")
    return Panel(Group(g, Text(""), gt), title=title,
                 subtitle="stale" if stale else None,
                 border_style="yellow" if stale else "blue")


def proc_table(hosts, max_rows) -> Table:
    t = Table(title="GPU processes (by memory)", title_justify="left", title_style="bold",
              box=None, padding=(0, 1), header_style="dim", expand=True)
    for c, kw in (("Host", {}), ("GPU", {"justify": "right"}), ("User", {}),
                  ("PID", {"justify": "right"}), ("GPU mem", {"justify": "right"}),
                  ("Src", {}), ("Time", {"justify": "right"}), ("Command", {"overflow": "ellipsis", "no_wrap": True, "ratio": 1})):
        t.add_column(c, **kw)
    rows = sorted(((h, p) for h in hosts if h.ok for p in h.procs), key=lambda x: -x[1].mem)
    for h, p in rows[:max(max_rows, 1)]:
        t.add_row(h.name or h.addr, str(p.gpu), Text(p.user, style="magenta"), str(p.pid),
                  gib(p.mem),
                  Text(f"slurm {p.job}", style="cyan") if p.job else Text("manual", style="yellow"),
                  p.etime, short_cmd(p.cmd))
    if len(rows) > max_rows:
        t.caption = f"... {len(rows) - max_rows} more (enlarge the terminal)"
    if not rows:
        t.add_row("", "", "no GPU processes", "", "", "", "", "")
    return t


def render(hosts, console, paused, interval):
    cols = 2 if console.width >= 150 else 1
    pw = console.width // cols
    bw = max(8, min(24, (pw - 36) // 2))
    panels = [host_panel(h, bw) for h in hosts]
    grid = Table.grid(expand=True)
    for _ in range(cols):
        grid.add_column(ratio=1)
    for i in range(0, len(panels), cols):
        row = panels[i:i + cols]
        grid.add_row(*row, *[""] * (cols - len(row)))

    ngpu = sum(len(h.gpus) for h in hosts)
    tot = sum(g.mem_total for h in hosts for g in h.gpus)
    used = sum(g.mem_used for h in hosts for g in h.gpus)
    avg = sum(g.util for h in hosts for g in h.gpus) / ngpu if ngpu else 0
    free = [(h, g) for h in hosts if h.ok for g in h.gpus if gpu_state(g, h.procs)[0] == "free"]
    head = Text()
    head.append(" clustop ", style="bold black on cyan")
    head.append(f" {PROFILE} " if PROFILE else "", style="bold cyan")
    head.append(f"  {sum(h.ok for h in hosts)}/{len(hosts)} hosts  ·  {ngpu} GPUs  ·  "
                f"avg util {avg:.0f}%  ·  VRAM {gib(used)}/{gib(tot)}")
    head.append("  ·  free: ", style="")
    head.append(", ".join(f"{h.name}:{g.idx}" for h, g in free) or "none", style="bold green" if free else "red")
    head.append(f"   q quit · space pause · +/- {interval:g}s" + (" PAUSED" if paused else ""), style="dim")

    used_rows = sum(3 + (0 if not h.ok else 5 + max(len(h.gpus), 1)) for h in hosts[::cols])
    free = console.height - used_rows - 6
    return Group(head, grid, proc_table(hosts, free))


# --------------------------------------------------------------------- main

def main():
    global PROFILE
    if len(sys.argv) > 1 and sys.argv[1] == "profile":
        return config.profile_cli(sys.argv[2:])

    ap = argparse.ArgumentParser(
        prog="clustop", description=__doc__.split("\n\n")[0],
        epilog="Manage profiles with: clustop profile {list,add,remove,default,show,path} ...")
    ap.add_argument("hosts", nargs="*", help="ad-hoc ssh hosts (ignores profiles)")
    ap.add_argument("-p", "--profile", help="profile to use (default: the default profile)")
    ap.add_argument("-u", "--user", help="ssh user (overrides the profile)")
    ap.add_argument("-i", "--interval", type=float, help="refresh seconds (default: profile or 2)")
    a = ap.parse_args()

    cfg = config.load()
    if a.hosts:
        PROFILE, prof = "", {"hosts": a.hosts}
    else:
        PROFILE, prof = config.pick(cfg, a.profile)
    interval = a.interval or prof.get("interval") or 2.0
    user = a.user or prof.get("user")

    if not sys.stdin.isatty():
        sys.exit("clustop needs an interactive terminal")
    hosts = []
    for addr in prof["hosts"]:
        h = HostState(addr.split("@")[-1])
        h.target = addr if "@" in addr or not user else f"{user}@{addr}"
        hosts.append(h)
    for h in hosts:  # sequential: password prompts need the terminal
        if not connect(h.target):
            h.error = "could not connect (wrong password / unreachable)"

    console = Console()
    stop, paused, ivl = threading.Event(), [False], [max(0.5, interval)]

    def worker():
        while not stop.is_set():
            if not paused[0]:
                ts = [threading.Thread(target=poll, args=(h,)) for h in hosts]
                [t.start() for t in ts]; [t.join() for t in ts]
            stop.wait(ivl[0])

    threading.Thread(target=worker, daemon=True).start()
    fd, old = sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setcbreak(fd)
        with Live(render(hosts, console, False, ivl[0]), console=console,
                  screen=True, auto_refresh=False) as live:
            while True:
                if select.select([sys.stdin], [], [], 0.5)[0]:
                    k = sys.stdin.read(1)
                    if k in ("q", "Q", "\x03"):
                        break
                    if k == " ":
                        paused[0] = not paused[0]
                    elif k in "+=":
                        ivl[0] = min(60.0, ivl[0] + (0.5 if ivl[0] < 5 else 1))
                    elif k in "-_":
                        ivl[0] = max(0.5, ivl[0] - (0.5 if ivl[0] <= 5 else 1))
                live.update(render(hosts, console, paused[0], ivl[0]), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
