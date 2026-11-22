#!/usr/bin/env python3
"""idle-reaper: turns off the simulators and local servers a Claude session started, once idle.

Eight simulators that Claude sessions had booted headless ran for days and overheated the Mac
(2026-11-16). Shutting everything down at the end of each turn was no answer either: the next
request boots it again. So a timer decides, and only for what Claude started.

  status                                  what is on, who started it, when it turns off
                                          (this is what the `running` command prints)
  off [port|simulator]                    turn off what Claude started, now (all of it, or one)
  keep <port|simulator> [hours|forever]   leave this one on (8 hours unless told otherwise)
  release <port|simulator>                undo keep
  set idle|limit <minutes>                change a timer
  pause [hours] / resume                  stop the timer for a while / start it again
  tick                                    one pass of the timer; launchd runs it every minute
  hook                                    one Claude Code hook event on stdin

The rules live in `sim_deadline` and `server_deadline`:
  A simulator goes off `idle_minutes` after the chat that booted it goes quiet, or `limit_minutes`
  after anything last touched it while that chat carries on.
  A server goes off `idle_minutes` after its chat goes quiet; a connected browser tab stretches
  that to `limit_minutes` at most. One that is working (using the processor) is never stopped.
  What Claude did not start is never touched.
"""
import fcntl
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("IDLE_REAPER_HOME") or HOME / ".claude" / "idle-reaper")
CONFIG, STATE, LOG, LOCK = DATA / "config.json", DATA / "state.json", DATA / "reaper.log", DATA / ".lock"
DEVICES = HOME / "Library/Developer/CoreSimulator/Devices"
PROJECTS = HOME / ".claude" / "projects"
XCODE = "/Applications/Xcode.app/Contents/Developer"
AGENT = "local.idle-reaper"
DEFAULTS = {"enabled": True, "idle_minutes": 10, "limit_minutes": 30, "dry_run": False}
FOREVER = 4102444800  # the year 2100

UDID = r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}"
UDID_RE = re.compile(UDID)
# A Bash command that may boot or use a simulator: the hook looks no further at any other.
SIMISH = re.compile(r"\bsimctl\b|\bxcodebuild\b|\bSimulator\b|\bexpo\b|\bflutter\b|react-native|\bmaestro\b|\bfastlane\b")
# One that boots whichever simulator it resolves to, without having to name it.
BOOTS = re.compile(r"\bxcodebuild\b|simctl\s+boot|\bSimulator\b|\bexpo\b|\bflutter\b|react-native|\bmaestro\b|\bfastlane\b")
# One that does something with a simulator. Listing them is not using them.
USES = re.compile(r"\bxcodebuild\b|simctl\s+(?!list\b|help\b)[a-z]|\bSimulator\b|\bexpo\b|\bflutter\b|react-native|\bmaestro\b|\bfastlane\b")
# `simctl launch booted ...`: whichever simulator is on, without naming it.
ALIAS = re.compile(r"simctl\s+(?!list\b)[a-z][^|;&\n]*\bbooted\b")
# Claude's own plumbing, never a server to stop even if it carries the session marker.
PLUMBING = re.compile(r"\bmcp\b|mcp[-_]server|modelcontextprotocol|language[-_]server|native[-_]host", re.I)
# A prompt the harness wrote (a finished background task, a reminder), not one the user typed.
AUTOMATIC = ("<task-notification", "<system-reminder", "<local-command", "[Artifact comment",
             "[SYSTEM NOTIFICATION")
NOTE = (
    "\n[idle-reaper] Stopped after {minutes} quiet minutes to save battery: the chat that started "
    "this had gone quiet and nothing was connected. This is expected, not a crash. Do not restart "
    "it on your own; start it again when the user's next request needs it. `running keep <port>` "
    "pins a server.\n"
)


# MARK: Small helpers

def run(args, timeout=20, env=None):
    full = dict(os.environ, LC_ALL="C")
    full.update(env or {})
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=full).stdout
    except Exception:
        return ""


def log(message):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.write_text(LOG.read_text(errors="replace")[-200_000:])
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except Exception:
        pass
    return cfg


def empty_state():
    return {"sims": {}, "left_alone": {}, "servers": {}, "not_claude": {}, "crumbs": [],
            "sessions": {}, "tombstones": [], "history": [], "floor": 0, "last_tick": 0,
            "paused_until": 0}


def load_state():
    state = empty_state()
    try:
        state.update(json.loads(STATE.read_text()))
    except Exception:
        pass
    return state


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


class Lock:
    """One writer at a time: the timer, the hooks and the commands all rewrite one state file."""

    def __init__(self, wait=5.0):
        self.wait, self.fd = wait, None

    def __enter__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    os.close(self.fd)
                    raise TimeoutError("idle-reaper state is busy")
                time.sleep(0.05)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def cpu_seconds(text):
    """`ps -o time`: minutes:seconds.hundredths, sometimes with hours or days in front."""
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        days = int(head)
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total + days * 86400


def span(seconds):
    minutes = int(max(0, seconds) // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return "%d min" % minutes
    return "%d h %d min" % (minutes // 60, minutes % 60) if minutes % 60 else "%d h" % (minutes // 60)


def program(cmd):
    """The executable's path. A command line does not say where the path ends and the arguments
    begin ("/Applications/Visual Studio Code.app/..."), so the shortest prefix that is a file wins."""
    if not cmd.startswith("/") or " " not in cmd:
        return cmd.split(" ", 1)[0]
    cut = cmd.find(" ")
    while cut != -1:
        if os.path.isfile(cmd[:cut]):
            return cmd[:cut]
        cut = cmd.find(" ", cut + 1)
    return cmd if os.path.isfile(cmd) else cmd.split(" ", 1)[0]


def short(cmd, width=46):
    path = program(cmd)
    text = (os.path.basename(path) + cmd[len(path):]).replace(str(HOME), "~")
    return text if len(text) <= width else text[: width - 1] + "…"


def tilde(path, width=70):
    text = (path or "").replace(str(HOME), "~")
    return text if len(text) <= width else "…" + text[-(width - 1):]


# MARK: What is running

class Table:
    """One `ps` of everything: parents, start times, processor time, command lines."""

    def __init__(self):
        self.rows, self.kids, self._start = {}, {}, {}
        for line in run(["/bin/ps", "-axo", "pid=,ppid=,time=,lstart=,command="]).splitlines():
            f = line.split(None, 8)
            if len(f) < 8:
                continue
            try:
                pid, ppid = int(f[0]), int(f[1])
            except ValueError:
                continue
            self.rows[pid] = (ppid, f[2], " ".join(f[3:8]), f[8] if len(f) > 8 else "")
            self.kids.setdefault(ppid, []).append(pid)

    def has(self, pid):
        return pid in self.rows

    def ppid(self, pid):
        return self.rows[pid][0] if pid in self.rows else 0

    def cmd(self, pid):
        return self.rows[pid][3] if pid in self.rows else ""

    def exe(self, pid):
        return os.path.basename(program(self.cmd(pid)))

    def cpu(self, pid):
        try:
            return cpu_seconds(self.rows[pid][1])
        except (KeyError, ValueError):
            return 0.0

    def start(self, pid):
        if pid not in self._start:
            try:
                self._start[pid] = time.mktime(time.strptime(self.rows[pid][2], "%a %b %d %H:%M:%S %Y"))
            except Exception:
                self._start[pid] = 0.0
        return self._start[pid]

    def tree(self, pid):
        """`pid` first, then everything beneath it."""
        out, stack = [], [pid]
        while stack:
            p = stack.pop()
            if p in self.rows and p not in out:
                out.append(p)
                stack.extend(self.kids.get(p, []))
        return out

    def key(self, pid):
        return "%d:%d" % (pid, self.start(pid))


def booted_sims(table):
    """udid -> pid of its `launchd_sim`, which lives exactly as long as the simulator is on."""
    out = {}
    for pid, row in table.rows.items():
        if "launchd_sim" in row[3][:160]:
            m = re.search(r"CoreSimulator/Devices/(%s)/" % UDID, row[3])
            if m:
                out[m.group(1)] = pid
    return out


def device_name(udid):
    try:
        with open(DEVICES / udid / "device.plist", "rb") as f:
            return plistlib.load(f).get("name") or udid
    except Exception:
        return udid


def sockets():
    """pid -> (ports it listens on, local ports that carry a live connection)."""
    out, pid, name = {}, None, ""
    for line in run(["/usr/sbin/lsof", "-nP", "-iTCP", "-F", "pnT"]).splitlines():
        kind, value = line[:1], line[1:]
        if kind == "p":
            pid = int(value) if value.isdigit() else None
        elif kind == "n":
            name = value
        elif kind == "T" and value.startswith("ST=") and pid is not None:
            port = name.split("->", 1)[0].rsplit(":", 1)[-1]
            if not port.isdigit():
                continue
            listening, live = out.setdefault(pid, (set(), set()))
            if value == "ST=LISTEN":
                listening.add(int(port))
            elif value == "ST=ESTABLISHED" and "->" in name:
                live.add(int(port))
    return out


def claude_marks(pids, hidden=None):
    """pid -> (session id, claude pid) for each process a Claude tool call started.

    The Bash tool puts CLAUDECODE=1, its session id and its own pid into the environment of
    everything it runs, and children inherit that even after their parent is gone. A server
    started from a terminal, an editor or Xcode carries none of it.

    `hidden`, when given, collects the processes whose environment `ps` cannot see at all: one
    that renamed itself ("next-server (v16.1.1)") has written its new title over it.
    """
    out = {}
    if not pids:
        return out
    listing = run(["/bin/ps", "eww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))])
    for line in listing.splitlines():
        m = re.match(r"\s*(\d+)\s", line)
        if m and hidden is not None and not re.search(r"\s(?:HOME|PATH)=", line):
            hidden.add(int(m.group(1)))
        if not m or not re.search(r"\sCLAUDECODE=1(\s|$)", line):
            continue
        sid = re.search(r"\sCLAUDE_CODE_SESSION_ID=(\S+)", line)
        owner = re.search(r"\sCLAUDE_PID=(\d+)", line)
        out[int(m.group(1))] = (sid.group(1) if sid else None, int(owner.group(1)) if owner else None)
    return out


def front_app():
    asn = run(["/usr/bin/lsappinfo", "front"], timeout=5).strip()
    if not asn:
        return ""
    m = re.search(r'"([^"]*)"', run(["/usr/bin/lsappinfo", "info", "-only", "name", asn], timeout=5))
    return m.group(1) if m else ""


# MARK: Chats

def note_session(state, data, now):
    sid = data.get("session_id")
    if not sid:
        return None
    info = state["sessions"].setdefault(sid, {})
    info["seen"] = now
    if data.get("transcript_path"):
        info["transcript"] = data["transcript_path"]
    if data.get("cwd"):
        info["cwd"] = data["cwd"]
    return info


def session_activity(state, sid):
    """When that chat last did anything: its transcript (and its subagents') is written as it works."""
    if not sid:
        return 0
    info = state["sessions"].setdefault(sid, {})
    path = info.get("transcript")
    if not path or not os.path.exists(path):
        hits = list(PROJECTS.glob("*/%s.jsonl" % sid))
        path = info["transcript"] = str(hits[0]) if hits else None
    newest = info.get("seen", 0)
    if path:
        try:
            newest = max(newest, os.path.getmtime(path))
            for entry in os.scandir(Path(path[:-len(".jsonl")]) / "subagents"):
                newest = max(newest, entry.stat().st_mtime)
        except OSError:
            pass
    return newest


# A Bash tool call: Claude Code runs each one in a shell that sources its snapshot of your shell.
TOOL_SHELL = re.compile(r"^/bin/(?:zsh|bash|sh) -c .*\.claude/shell-snapshots/")


def claude_ancestor(table, pid):
    """The Claude Code process above `pid`, if any."""
    for _ in range(40):
        if pid <= 1 or not table.has(pid):
            return None
        if table.exe(pid) == "claude":
            return pid
        pid = table.ppid(pid)
    return None


def chat_busy(state, sid, table, now):
    """Whether that chat is in the middle of a tool call: a long build or test writes nothing to
    the transcript until it ends. A foreground call lasts ten minutes at most, so a shell older
    than eleven is a background task, which stops counting then."""
    info = state["sessions"].get(sid) or {}
    pid = info.get("pid")
    if table is None or not pid or not table.has(pid) or table.exe(pid) != "claude":
        return False
    if abs(table.start(pid) - info.get("pid_start", 0)) > 2:
        return False  # that Claude process is gone and the number was reused
    return any(TOOL_SHELL.search(table.cmd(kid)) and now - table.start(kid) < 660
               for kid in table.kids.get(pid, []))


def chat_last(state, sid, table=None):
    """When that chat last did anything, or now if it is busy right now."""
    now = time.time()
    return now if sid and chat_busy(state, sid, table, now) else session_activity(state, sid)


def chat_name(state, sid):
    if not sid:
        return "unknown chat"
    session_activity(state, sid)
    info = state["sessions"].get(sid) or {}
    if not info.get("cwd") and info.get("transcript"):
        try:
            with open(info["transcript"], "r", errors="replace") as f:
                m = re.search(r'"cwd"\s*:\s*"([^"]+)"', f.read(40000))
            if m:
                info["cwd"] = m.group(1)
        except OSError:
            pass
    return os.path.basename(info["cwd"].rstrip("/")) if info.get("cwd") else sid[:8]


# MARK: Simulators

def names(cmd, name):
    """Whether `cmd` names this simulator: "iPhone 17 Pro" is not "iPhone 17 Pro (Claude)" or "... Max"."""
    return bool(name) and re.search(re.escape(name) + r"(?!\s*[\w(])", cmd) is not None


def attribute(state, udid, name, boot):
    """Who booted the simulator that came on at `boot`: (session, reason), or (None, None) for not Claude.

    The hook leaves a crumb before every simulator command a Claude session runs, with the
    simulators already on at that moment. A simulator that was off then and came on while the
    command ran (or within fifteen minutes of one sent to the background) was booted by it,
    provided the command names this simulator or names none at all.
    """
    for crumb in reversed(state["crumbs"]):
        was = crumb["booted"]  # udid -> when it had come on; one shut down and booted again is new
        if udid in was and (not isinstance(was, dict) or abs(was[udid] - boot) <= 2):
            continue
        closed = crumb.get("end") and not crumb.get("bg")
        last = crumb["end"] + 30 if closed else crumb["t"] + 900
        if not crumb["t"] - 5 <= boot <= last:
            continue
        cmd = crumb["cmd"]
        this_one = udid in cmd or names(cmd, name)
        another = not this_one and (UDID_RE.search(cmd) or "name=" in cmd)
        if this_one or (not another and BOOTS.search(cmd)):
            return crumb.get("session"), "a Claude command booted it"
    if "claude" in name.lower():
        return None, "its name says a Claude session made it"
    return None, None


# How long a simulator that nothing explains yet waits before it is judged the user's: the script
# that booted it names it a moment later (`simctl bootstatus`, `xcodebuild -destination`).
JUDGE_WINDOW = 180


def device_os(udid):
    """"27.0" for a simulator on iOS 27.0, read from its runtime identifier."""
    try:
        with open(DEVICES / udid / "device.plist", "rb") as f:
            runtime = plistlib.load(f).get("runtime") or ""
    except Exception:
        return ""
    m = re.search(r"-(\d+)-(\d+)$", runtime)
    return "%s.%s" % m.groups() if m else ""


def booted_by_script(table, udid, name, boot):
    """(True, session) when a chat's running command names this simulator and began before it came on.

    A chat's own script (`check.sh`, which runs `xcrun simctl boot`) never names a simulator tool
    in the command the chat typed, so the hook leaves no crumb for it. The tools the script runs
    carry the chat's marker and name the device, and the Bash call they belong to began before
    the device came on. A device the user booted, that a chat only reaches for afterwards, fails
    that last test and stays the user's.
    """
    version = device_os(udid)

    def means_it(cmd):
        if udid in cmd:
            return True
        if not names(cmd, name):
            return False
        given = re.search(r"\bOS=(\d+(?:\.\d+)*)", cmd)  # two "iPad Pro 11-inch (M5)" on two systems
        return not given or not version or version == given.group(1) or version.startswith(given.group(1) + ".")

    named = [pid for pid in table.rows if pid != os.getpid() and means_it(table.cmd(pid))]
    chain = set()
    for pid in named:
        while pid > 1 and table.has(pid) and pid not in chain:
            chain.add(pid)
            pid = table.ppid(pid)
    marks = claude_marks(sorted(chain))
    for pid in named:
        if pid not in marks:
            continue
        sid = marks[pid][0]
        top = pid  # the Bash call it belongs to: climb to the last ancestor with the same marker
        while True:
            parent = table.ppid(top)
            if parent <= 1 or parent not in marks or marks[parent][0] != sid or table.exe(parent) == "claude":
                break
            top = parent
        if table.start(top) <= boot + 2:
            return True, sid
    return None


def refresh_sims(state, table, now):
    booted = booted_sims(table)
    for book in ("sims", "left_alone"):
        for udid in list(state[book]):
            entry = state[book][udid]
            boot = entry["boot"] if book == "sims" else entry
            if udid not in booted or abs(table.start(booted[udid]) - boot) > 2:
                del state[book][udid]  # off, or booted afresh: judged again
    for udid, pid in booted.items():
        if udid in state["sims"] or udid in state["left_alone"]:
            continue
        boot, name = table.start(pid), device_name(udid)
        session, why = attribute(state, udid, name, boot)
        if not why:
            found = booted_by_script(table, udid, name, boot)
            if found:
                session, why = found[1], "a script a Claude command ran booted it"
        if why:
            state["sims"][udid] = {"name": name, "boot": boot, "owned_at": now, "last_used": now,
                                   "sessions": [session] if session else [], "why": why,
                                   "keep_until": 0}
            log("simulator %s (%s) is Claude's: %s" % (name, udid[:8], why))
        elif now - boot >= JUDGE_WINDOW:
            state["left_alone"][udid] = boot
        # else: too soon to say; the script that booted it may not have named it yet
    return booted


def touch_sims(state, sid, cmd, now):
    """A simulator command a chat just ran counts as using the simulators it means."""
    if not USES.search(cmd):
        return
    mentioned = set(UDID_RE.findall(cmd))
    names_one = bool(mentioned) or "name=" in cmd
    only_one_on = len(state["sims"]) + len(state["left_alone"]) == 1
    for udid, entry in state["sims"].items():
        alias = not names_one and ALIAS.search(cmd) is not None
        hit = udid in mentioned or names(cmd, entry["name"]) or (alias and only_one_on)
        loose = not names_one and (sid in entry["sessions"] or alias)
        if hit or loose:
            entry["last_used"] = now
        if hit and sid and sid not in entry["sessions"]:
            entry["sessions"].append(sid)


def sim_in_use(udid, entry, table, booted, front):
    if front == "Simulator":
        return "you have Simulator in front"
    for pid in table.tree(booted[udid]):
        if re.search(r"-Runner\.app/|/xctest(\s|$)|XCTRunner", table.cmd(pid)):
            return "a test is running on it"
    for pid in table.rows:
        cmd = table.cmd(pid)
        if "xcodebuild" not in cmd or table.exe(pid) != "xcodebuild":
            continue
        if udid in cmd or names(cmd, entry["name"]):
            return "xcodebuild is using it"
        if "platform=macOS" in cmd and "Simulator" not in cmd:
            continue
        if not UDID_RE.search(cmd) and "name=" not in cmd and re.search(r"\btest|-destination", cmd):
            return "xcodebuild is running"
    return None


def sim_deadline(state, entry, cfg, table=None):
    """(when it goes off, what will have been true by then, since when)."""
    idle, limit = cfg["idle_minutes"] * 60, cfg["limit_minutes"] * 60
    used = max(entry["last_used"], entry["owned_at"], state["floor"])
    chat = max([chat_last(state, s, table) for s in entry["sessions"]] or [0])
    if chat <= used:
        return used + idle, "nothing has used it for %s", used
    if used + limit < chat + idle:
        return used + limit, "nothing has touched it for %s, though its chat is still going", used
    return chat + idle, "its chat has been quiet for %s", chat


# MARK: Servers

def cpu_busy(samples, now):
    """Whether the task averaged over a tenth of a core lately; None until four minutes are on record."""
    if not samples or now - samples[0][0] < 240:
        return None
    return (samples[-1][1] - samples[0][1]) / (now - samples[0][0]) > 0.10


def restarted_unasked(state, entry, now):
    """Why a new server must not be stopped again yet, or None.

    Stopping a background task wakes its chat, and a chat that knows nothing of this timer may
    start the server again: stop, wake, restart, every half hour, a turn each time. So one that
    comes back with no request from the user in between is left on until the user next writes
    there, and one already stopped twice in a day is left on for the rest of it.
    """
    sid = entry.get("session")
    stones = [s for s in state["tombstones"]
              if s["session"] == sid and now - s["t"] < 86400
              and (set(s["ports"]) & set(entry["ports"])
                   or (s.get("cwd") and s["cwd"] == entry.get("cwd") and s.get("cmd") == entry.get("cmd")))]
    if not stones:
        return None
    if len(stones) >= 2:
        return {"why": "stopped twice today already, so left on", "until": now + 86400}
    asked = (state["sessions"].get(sid) or {}).get("prompt", 0)
    if asked < max(s["t"] for s in stones):
        return {"why": "its chat restarted it without being asked, so left on until you next write there",
                "until_prompt": True}
    return None


def refresh_servers(state, table, socks, now):
    listening = {pid: ports for pid, (ports, _) in socks.items() if ports and table.has(pid)}
    for key in list(state["not_claude"]):
        pid = int(key.split(":")[0])
        if pid not in listening or table.key(pid) != key:
            del state["not_claude"][key]
    fresh = [pid for pid in listening if table.key(pid) not in state["not_claude"]]
    chain = set()
    for pid in fresh:
        while pid > 1 and table.has(pid) and pid not in chain:
            chain.add(pid)
            pid = table.ppid(pid)
    hidden = set()
    marks = claude_marks(sorted(chain), hidden)
    for pid in fresh:
        # A server that renames itself ("next-server (v16.1.1)") writes its title over the memory
        # `ps` reads the environment from, Claude's marker and all. Whatever started it still has it.
        up = pid
        while up in hidden and up not in marks:
            up = table.ppid(up)
        if pid not in marks and up in marks:
            marks[pid] = marks[up]
    alive = set()
    for pid in fresh:
        if pid not in marks or table.exe(pid) == "claude" or PLUMBING.search(table.cmd(pid)):
            state["not_claude"][table.key(pid)] = 1
            continue
        sid, owner = marks[pid]
        if sid and owner and table.exe(owner) == "claude":
            info = state["sessions"].setdefault(sid, {})
            info["pid"], info["pid_start"] = owner, table.start(owner)
        root = pid  # the task is everything that one Bash call started: climb to its top
        while True:
            parent = table.ppid(root)
            if (parent <= 1 or parent == owner or parent not in marks
                    or marks[parent][0] != sid or table.exe(parent) == "claude"):
                break
            root = parent
        key = table.key(root)
        entry = state["servers"].get(key)
        if entry is None:
            cwd = re.search(r"^n(.*)$", run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]), re.M)
            entry = state["servers"][key] = {
                "pid": root, "start": table.start(root), "ports": sorted(listening[pid]),
                "cmd": table.cmd(pid)[:300], "cwd": cwd.group(1) if cwd else "", "session": sid,
                "claude_pid": owner, "first_seen": now, "last_connected": 0, "last_busy": 0,
                "cpu": [], "keep_until": 0, "exempt": None}
            entry["exempt"] = restarted_unasked(state, entry, now)
            log("server on %s (%s) is Claude's, from chat %s%s" % (
                entry["ports"], short(entry["cmd"]), (sid or "?")[:8],
                "; " + entry["exempt"]["why"] if entry["exempt"] else ""))
        entry["ports"] = sorted(set(entry["ports"]) | listening[pid])
        alive.add(key)
    for key in list(state["servers"]):
        if key not in alive:
            del state["servers"][key]


def server_deadline(state, entry, cfg, table):
    """(when it goes off, what will have been true by then, since when), or (None, why it is left on, 0)."""
    idle, limit = cfg["idle_minutes"] * 60, cfg["limit_minutes"] * 60
    sid, owner = entry.get("session"), entry.get("claude_pid")
    if not sid and owner and table.exe(owner) == "claude":
        return None, "its Claude session is open, but which chat it is cannot be told", 0
    chat = max(entry["first_seen"], state["floor"], chat_last(state, sid, table))
    off, why = chat + idle, "its chat has been quiet for %s"
    if entry["last_connected"]:
        held = min(entry["last_connected"] + idle, chat + limit)
        if held > off:
            off, why = held, "its chat has been quiet for %s, and a connected tab only holds it so long"
    if entry["last_busy"] + idle > off:
        off = entry["last_busy"] + idle
    return off, why, chat


# MARK: One look at everything

def look():
    """The slow part of a survey: `ps`, `lsof` and the app in front. Done before the lock is taken,
    since on a busy Mac these take seconds, and every chat's hook would be left waiting on them.
    The app in front only matters to a simulator, so it is not asked for when none is on."""
    table = Table()
    return table, sockets(), front_app() if booted_sims(table) else ""


def survey(state, now, seen=None):
    """Bring the state up to date with what is on right now, and note what is in use. `seen` is
    what `look()` found; pass it in to keep the slow part out of the lock."""
    table, socks, front = seen or look()
    booted = refresh_sims(state, table, now)
    refresh_servers(state, table, socks, now)
    front = front if state["sims"] else ""
    for udid, entry in state["sims"].items():
        entry["doing"] = sim_in_use(udid, entry, table, booted, front)
        if entry["doing"]:
            entry["last_used"] = now
    for entry in state["servers"].values():
        tree = table.tree(entry["pid"])
        entry["cpu"] = [s for s in entry["cpu"] if now - s[0] <= 600] + [[now, sum(table.cpu(p) for p in tree)]]
        ports = set(entry["ports"])
        entry["doing"] = None
        if any(socks.get(p, (set(), set()))[1] & ports for p in tree):
            entry["last_connected"], entry["doing"] = now, "a browser tab or client is connected"
        if cpu_busy(entry["cpu"], now):
            entry["last_busy"], entry["doing"] = now, "it is working (using the processor)"
        if any(table.exe(p) == "claude" for p in tree):
            entry["doing"] = "a Claude session runs inside it"
    return table


def due(state, cfg, table, now):
    """What the timer turns off on this pass."""
    plan = []
    for udid, entry in state["sims"].items():
        if entry["doing"] or entry["keep_until"] > now:
            continue
        off, why, since = sim_deadline(state, entry, cfg, table)
        if now >= off:
            plan.append({"kind": "sim", "key": udid, "what": entry["name"], "why": why % span(now - since)})
    for key, entry in state["servers"].items():
        exempt = entry.get("exempt")
        if exempt and exempt.get("until", FOREVER) < now and not exempt.get("until_prompt"):
            exempt = entry["exempt"] = None
        if entry["doing"] or entry["keep_until"] > now or exempt:
            continue
        if cpu_busy(entry["cpu"], now) is None:
            continue  # too new to know whether it is working
        off, why, since = server_deadline(state, entry, cfg, table)
        if off is not None and now >= off:
            plan.append({"kind": "server", "key": key, "why": why % span(now - since),
                         "what": "port %s (%s)" % (", ".join(map(str, entry["ports"])), short(entry["cmd"], 40))})
    return plan


def prune(state, now):
    state["crumbs"] = [c for c in state["crumbs"] if now - c["t"] < 7200][-200:]
    state["tombstones"] = [s for s in state["tombstones"] if now - s["t"] < 86400]
    state["history"] = state["history"][-30:]
    used = {s for e in state["sims"].values() for s in e["sessions"]}
    used |= {e.get("session") for e in state["servers"].values()}
    used |= {s["session"] for s in state["tombstones"]}
    for sid in list(state["sessions"]):
        info = state["sessions"][sid]
        if sid not in used and now - max(info.get("seen", 0), info.get("prompt", 0)) > 7 * 86400:
            del state["sessions"][sid]


# MARK: Turning things off

def shutdown_sim(udid):
    env = {"DEVELOPER_DIR": XCODE} if os.path.isdir(XCODE) else {}
    full = dict(os.environ, LC_ALL="C")
    full.update(env)
    try:
        done = subprocess.run(["/usr/bin/xcrun", "simctl", "shutdown", udid], capture_output=True,
                              text=True, timeout=120, env=full)
    except Exception as error:
        return False, str(error)
    ok = done.returncode == 0 or "current state: Shutdown" in done.stderr
    return ok, done.stderr.strip()


def leave_note(pids, minutes):
    """Tell the chat why: a stopped background task wakes it, and it reads the task's output."""
    listing = run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-d", "1,2", "-F", "tn"])
    files, kind = set(), ""
    for line in listing.splitlines():
        if line[:1] == "t":
            kind = line[1:]
        elif line[:1] == "n" and kind == "REG" and re.search(r"/tasks/[^/]+\.output$", line[1:]):
            files.add(line[1:])
    for path in files:
        try:
            with open(path, "a") as f:
                f.write(NOTE.format(minutes=minutes))
        except OSError:
            pass


def stop_server(entry, minutes):
    table = Table()
    root = entry["pid"]
    if not table.has(root) or abs(table.start(root) - entry["start"]) > 2:
        return True, "already gone"
    tree = table.tree(root)
    if os.getpid() in tree or any(table.exe(p) == "claude" for p in tree):
        return False, "a Claude session runs inside it"
    leave_note(tree, minutes)
    for pid in tree:  # the top first, so nothing above respawns what is stopped below
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 6
    while time.time() < deadline and any(_alive(p) for p in tree):
        time.sleep(0.3)
    left = [p for p in tree if _alive(p)]
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return True, "needed a hard stop" if left else ""


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def carry_out(plan, cfg):
    """The slow part, with the state unlocked; the outcome is written back afterwards."""
    with Lock():
        state = load_state()
    done = []
    for item in plan:
        if item["kind"] == "sim":
            ok, detail = shutdown_sim(item["key"])
        else:
            entry = state["servers"].get(item["key"])
            ok, detail = stop_server(entry, cfg["idle_minutes"]) if entry else (True, "already gone")
        log("%s %s: %s%s" % ("turned off" if ok else "could NOT turn off", item["what"], item["why"],
                             " (%s)" % detail if detail else ""))
        if ok:
            done.append(item)
    now = time.time()
    with Lock():
        state = load_state()
        for item in done:
            state["history"].append({"t": now, "what": item["what"], "why": item["why"]})
            if item["kind"] == "sim":
                state["sims"].pop(item["key"], None)
            else:
                entry = state["servers"].pop(item["key"], None)
                if entry and not item.get("asked"):
                    state["tombstones"].append({"t": now, "session": entry.get("session"),
                                                "ports": entry["ports"], "cwd": entry.get("cwd"),
                                                "cmd": entry.get("cmd")})
        save_state(state)
    return done


# MARK: Commands

def tick():
    cfg = load_config()
    seen = look()
    # Taken after the look, not before: a Mac that sleeps partway through one wakes with the look
    # still to finish, and a time from before the sleep would be recorded as when this pass ran.
    now = time.time()
    with Lock(10):
        state = load_state()
        if state["last_tick"] and now - state["last_tick"] > 300:
            state["floor"] = now  # asleep, or stalled: nothing is idle for time nobody could use it
            log("back after %s away; every timer starts over" % span(now - state["last_tick"]))
        state["last_tick"] = now
        table = survey(state, now, seen)
        plan = due(state, cfg, table, now) if cfg["enabled"] and state["paused_until"] < now else []
        if cfg["dry_run"]:
            for item in plan:
                book = state["sims"] if item["kind"] == "sim" else state["servers"]
                if not book[item["key"]].get("dry"):
                    book[item["key"]]["dry"] = True
                    log("dry run, would turn off %s: %s" % (item["what"], item["why"]))
            plan = []
        prune(state, now)
        save_state(state)
    if plan:
        carry_out(plan, cfg)


def hook():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    now, sid, event = time.time(), data.get("session_id"), data.get("hook_event_name")
    if event == "UserPromptSubmit":
        head = (data.get("prompt") or "").lstrip()[:600]
        if head.startswith(AUTOMATIC) or "<task-notification>" in head:
            return
        with Lock(5):
            state = load_state()
            info = note_session(state, data, now)
            if info is not None:
                info["prompt"] = now
            for entry in state["servers"].values():
                if entry.get("session") == sid and (entry.get("exempt") or {}).get("until_prompt"):
                    entry["exempt"] = None
            save_state(state)
        return
    tool = data.get("tool_input") or {}
    cmd = tool.get("command") or ""
    if data.get("tool_name") != "Bash" or not SIMISH.search(cmd):
        return
    table = Table()
    with Lock(5):
        state = load_state()
        info = note_session(state, data, now)
        owner = claude_ancestor(table, os.getppid())
        if info is not None and owner:
            info["pid"], info["pid_start"] = owner, table.start(owner)
        ident = data.get("tool_use_id") or ""
        if event == "PreToolUse":
            state["crumbs"].append({
                "id": ident, "t": now, "end": None, "session": sid, "cmd": cmd[:1500],
                "bg": bool(tool.get("run_in_background")) or cmd.rstrip().endswith("&") or "nohup " in cmd,
                "booted": {u: table.start(p) for u, p in booted_sims(table).items()}})
        else:
            for crumb in reversed(state["crumbs"]):
                same = crumb["id"] == ident if ident and crumb["id"] else crumb["cmd"] == cmd[:1500]
                if crumb["session"] == sid and crumb["end"] is None and same:
                    crumb["end"] = now
                    break
        refresh_sims(state, table, now)
        touch_sims(state, sid, cmd, now)
        save_state(state)


def find(state, target):
    """A port, or part of a simulator's name or id, to ("sim" | "server", key)."""
    hits = []
    if target.isdigit():
        hits = [("server", k) for k, e in state["servers"].items() if int(target) in e["ports"]]
    else:
        low = target.lower()
        hits = [("sim", u) for u, e in state["sims"].items()
                if low in e["name"].lower() or u.lower().startswith(low)]
        exact = [h for h in hits if state["sims"][h[1]]["name"].lower() == low]
        hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    print("No single match for %r among what Claude started. `running` lists it." % target if not hits
          else "%r matches %d things; be more specific." % (target, len(hits)))
    return None


def agent_loaded():
    return subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), AGENT)],
                          capture_output=True).returncode == 0


KNOWN_PORTS = {5432: "Postgres", 3306: "MySQL", 6379: "Redis", 27017: "MongoDB", 9200: "Elasticsearch"}
# What a listening process is, in plain words, and whether it serves something you would open.
KINDS = (
    (r"gradle", "Gradle daemon (Android build helper)", False),
    (r"kotlin.?compile.?daemon|kotlin\.daemon", "Kotlin daemon (build helper)", False),
    (r"com\.docker|vpnkit", "Docker", True),
    (r"next-server|/next(\s|$)|\bnext\s+(dev|start)", "Next.js dev server", True),
    (r"\bvite\b", "Vite dev server", True),
    (r"\bastro\b", "Astro dev server", True),
    (r"webpack", "webpack dev server", True),
    (r"react-scripts", "React dev server", True),
    (r"\bexpo\b|\bmetro\b", "Expo / Metro bundler", True),
    (r"http\.server|simplehttpserver", "Python web server", True),
    (r"uvicorn|gunicorn|flask|runserver|fastapi|hypercorn", "Python app server", True),
    (r"jekyll", "Jekyll site", True),
    (r"\bhugo\b", "Hugo site", True),
    (r"\brails\b|\bpuma\b", "Rails server", True),
    (r"wrangler|workerd", "Cloudflare dev server", True),
    (r"postgres", "Postgres", True),
)
MACOS = ("rapportd", "ControlCenter", "sharingd", "AirPlayXPCHelper", "launchd", "remoted", "mDNSResponder")
# A program from a simulator's system image, or an app installed in one.
SIMULATOR_INSIDE = re.compile(r"\.simruntime/Contents/Resources/RuntimeRoot/|/CoreSimulator/Devices/%s/data/" % UDID)


def describe(cmd, exe, ports):
    low = cmd.lower()
    for pattern, label, web in KINDS:
        if re.search(pattern, low):
            if label == "Docker":
                dbs = [KNOWN_PORTS[p] for p in ports if p in KNOWN_PORTS]
                label += " (%s)" % ", ".join(dbs) if dbs else ""
            return label, web
    if exe == "java":
        return "Java helper", False
    if exe == "node":
        return "Node server", True
    if exe.lower().startswith("python"):
        return "Python server", True
    return short(cmd, 30), True


def folders(pids):
    """pid -> the folder it runs in."""
    out, pid = {}, None
    if not pids:
        return out
    for line in run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fpn"]).splitlines():
        if line[:1] == "p":
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line[:1] == "n" and pid:
            out[pid] = line[1:]
    return out


def status(show_all=False):
    cfg, now = load_config(), time.time()
    seen = look()
    with Lock():
        state = load_state()
        table = survey(state, now, seen)
        save_state(state)
    socks = seen[1]
    paused = state["paused_until"] > now or not cfg["enabled"]

    def countdown(off, why_left=""):
        if off is None:
            return "Left on: " + why_left
        if paused:
            return "Timer paused"
        return "Off any minute" if off <= now else "Off in " + span(off - now)

    def fate(entry, deadline):
        if entry["keep_until"] > now:
            return "Kept on" + ("" if entry["keep_until"] >= FOREVER else " for " + span(entry["keep_until"] - now))
        if (entry.get("exempt") or {}).get("why"):
            return "Left on: " + entry["exempt"]["why"]
        if entry["doing"]:
            return "In use: " + entry["doing"]
        return countdown(*deadline()[:2])

    def address(ports, web):
        if not web:
            return ""
        return "localhost:%d" % ports[0] + (" +%d" % (len(ports) - 1) if len(ports) > 1 else "")

    def where(path):
        return "" if not path or path == "/" else "in " + tilde(path)

    # Simulators
    mine_sims, your_sims = [], []
    for udid, pid in sorted(booted_sims(table).items(), key=lambda kv: device_name(kv[0])):
        entry = state["sims"].get(udid)
        if not entry:
            your_sims.append("    %-34s on for %s" % (device_name(udid), span(now - table.start(pid))))
            continue
        chats = ", ".join(chat_name(state, s) for s in entry["sessions"])
        mine_sims.append("    %-34s %-36s %s" % (
            entry["name"], fate(entry, lambda: sim_deadline(state, entry, cfg, table)),
            chats + " chat" if chats else "chat unknown"))

    # Servers
    owned = {}
    for entry in state["servers"].values():
        for p in table.tree(entry["pid"]):
            owned[p] = entry
    mine_srv, yours, helpers, seen = [], [], [], set()
    for pid, (ports, _) in sorted(socks.items(), key=lambda kv: min(kv[1][0]) if kv[1][0] else 0):
        if not ports or not table.has(pid):
            continue
        entry = owned.get(pid)
        if entry:
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            label, web = describe(entry["cmd"], os.path.basename(program(entry["cmd"])), entry["ports"])
            line = "    %-17s %-22s %-36s %s chat" % (
                address(entry["ports"], True), label,
                fate(entry, lambda: server_deadline(state, entry, cfg, table)),
                chat_name(state, entry.get("session")))
            mine_srv.append(line + ("\n    %-17s %s" % ("", where(entry["cwd"])) if where(entry["cwd"]) else ""))
            continue
        exe, cmd = table.exe(pid), table.cmd(pid)
        if exe == "claude":
            helpers.append(("Claude sessions", pid, ports, cmd))
        elif "Helper" in cmd[:200] or exe in ("Electron", "Code", "Cursor"):
            helpers.append(("editor helpers", pid, ports, cmd))
        elif SIMULATOR_INSIDE.search(cmd):
            # Runs inside a simulator (siriactionsd, an app under test) and goes when it goes.
            helpers.append(("simulator services", pid, ports, cmd))
        elif exe in MACOS:
            helpers.append(("macOS services", pid, ports, cmd))
        else:
            yours.append((pid, sorted(ports), exe, cmd))

    places = folders([y[0] for y in yours])
    your_srv, quiet = [], {}
    for pid, ports, exe, cmd in yours:
        label, web = describe(cmd, exe, ports)
        if web:
            your_srv.append(("    %-17s %-22s %s" % (address(ports, True), label, where(places.get(pid)))).rstrip())
        else:
            quiet[label] = quiet.get(label, 0) + 1
    for label, count in quiet.items():
        your_srv.append("    %s%s, not a website" % (label, ": %d running" % count if count > 1 else ""))

    def block(title, sims, servers):
        print(title)
        print("  Simulators")
        print("\n".join(sims) if sims else "    none")
        print("  Websites and servers")
        print("\n".join(servers) if servers else "    none")

    if paused:
        rule = "timer paused"
    else:
        rule = "turned off after %d quiet minutes" % cfg["idle_minutes"]
    block("Started by you (never touched)", your_sims, your_srv)
    print()
    block("Started by Claude (%s)" % rule, mine_sims, mine_srv)

    if helpers and show_all:
        print("\nBackground helpers (never touched)")
        for kind, pid, ports, cmd in helpers:
            print("    %-17s %-16s %s" % (":" + ",".join(map(str, sorted(ports))), kind, short(cmd, 50)))
    elif helpers:
        counts = {}
        for kind, *_ in helpers:
            counts[kind] = counts.get(kind, 0) + 1
        print("\nNot shown: %s. `running --all` lists them." % ", ".join("%d %s" % (n, k) for k, n in counts.items()))

    recent = [h for h in state["history"] if now - h["t"] < 86400]
    if recent:
        print("\nTurned off lately")
        for item in recent[-6:]:
            print("    %s  %s: %s" % (time.strftime("%a %H:%M", time.localtime(item["t"])), item["what"], item["why"]))

    timer = "\nTimer: Claude's things go off after %d quiet minutes, %d at most." % (
        cfg["idle_minutes"], cfg["limit_minutes"])
    if not agent_loaded():
        timer += " NOT RUNNING: the background job is not loaded."
    elif state["paused_until"] > now:
        timer += " Paused%s." % ("" if state["paused_until"] >= FOREVER else " for " + span(state["paused_until"] - now))
    elif not cfg["enabled"]:
        timer += " Switched off in config.json."
    elif cfg["dry_run"]:
        timer += " Dry run: it only logs what it would turn off."
    elif state["last_tick"]:
        timer += " Running (last look %s ago)." % span(now - state["last_tick"])
    print(timer)


def off(target):
    cfg, now = load_config(), time.time()
    seen = look()
    with Lock():
        state = load_state()
        survey(state, now, seen)
        save_state(state)
        if target:
            hit = find(state, target)
            if not hit:
                return
            chosen = [hit]
        else:
            chosen = [("sim", u) for u in state["sims"]] + [("server", k) for k in state["servers"]]
    plan = []
    for kind, key in chosen:
        entry = (state["sims"] if kind == "sim" else state["servers"])[key]
        what = entry["name"] if kind == "sim" else "port %s (%s)" % (
            ", ".join(map(str, entry["ports"])), short(entry["cmd"], 40))
        plan.append({"kind": kind, "key": key, "what": what, "why": "you asked", "asked": True})
    if not plan:
        print("Nothing Claude started is on.")
        return
    done = carry_out(plan, cfg)
    for item in plan:
        print("%s %s" % ("Turned off" if item in done else "Could not turn off", item["what"]))


def keep(target, amount, on=True):
    now = time.time()
    seen = look()
    with Lock():
        state = load_state()
        survey(state, now, seen)
        hit = find(state, target)
        if hit:
            entry = (state["sims"] if hit[0] == "sim" else state["servers"])[hit[1]]
            if not on:
                entry["keep_until"] = 0
            elif amount == "forever":
                entry["keep_until"] = FOREVER
            else:
                entry["keep_until"] = now + float(amount or 8) * 3600
            label = entry["name"] if hit[0] == "sim" else "port %d" % entry["ports"][0]
            print("%s: %s" % (label, "back on the timer" if not on else
                              "kept on" + ("" if amount == "forever" else " for %s" % span(entry["keep_until"] - now))))
        save_state(state)


def main(argv):
    command = argv[0] if argv else "status"
    rest = argv[1:]
    if command == "hook":
        try:
            hook()
        except Exception as error:  # a hook must never get in the way of the tool call
            log("hook error: %r" % (error,))
    elif command == "tick":
        try:
            tick()
        except TimeoutError:
            pass
        except Exception as error:
            log("tick error: %r" % (error,))
            raise
    elif command in ("status", "--all"):
        status(show_all="--all" in argv)
    elif command == "off":
        off(rest[0] if rest else None)
    elif command in ("keep", "release") and rest:
        keep(rest[0], rest[1] if len(rest) > 1 else None, on=command == "keep")
    elif command == "set" and len(rest) == 2 and rest[0] in ("idle", "limit") and rest[1].isdigit():
        cfg = {}
        try:
            cfg = json.loads(CONFIG.read_text())
        except Exception:
            pass
        cfg[rest[0] + "_minutes"] = int(rest[1])
        DATA.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=1) + "\n")
        print("%s is now %s minutes." % ("Quiet time" if rest[0] == "idle" else "The limit", rest[1]))
    elif command in ("pause", "resume"):
        now = time.time()
        with Lock():
            state = load_state()
            if command == "pause":
                state["paused_until"] = now + float(rest[0]) * 3600 if rest else FOREVER
            else:
                state["paused_until"], state["floor"] = 0, now
            save_state(state)
        print("Timer paused%s." % (" for %s hours" % rest[0] if rest else " until `running resume`")
              if command == "pause" else "Timer running again; every countdown starts over.")
    else:
        print(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
