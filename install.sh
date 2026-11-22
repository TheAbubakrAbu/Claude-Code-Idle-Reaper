#!/usr/bin/env bash
# Claude Code Idle Reaper installer (macOS).
#   ./install.sh                         install, wire the hooks, load the timer, add `running`
#   ./install.sh --dest ~/.claude/hooks  where idle-reaper.py and its hook shim go
#   ./install.sh --data DIR              where config.json, state.json and the log live
#   ./install.sh --settings FILE         the Claude Code settings.json to wire
#   ./install.sh --launch-agents DIR     where the LaunchAgent plist goes
#   ./install.sh --bin DIR               where the `running` command is linked
#   ./install.sh --no-launchd            write the LaunchAgent but do not load or unload it
#   ./install.sh --uninstall             remove the hooks, the timer and `running`, keep the files
#   ./install.sh --uninstall --purge     also delete idle-reaper.py, the shim, config, state, logs
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/hooks"
DATA="$HOME/.claude/idle-reaper"
SETTINGS="$HOME/.claude/settings.json"
AGENTS="$HOME/Library/LaunchAgents"
BIN=""
LAUNCHD=1
UNINSTALL=0
PURGE=0
LABEL="local.idle-reaper"

while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --settings) SETTINGS="$2"; shift 2 ;;
    --launch-agents) AGENTS="$2"; shift 2 ;;
    --bin) BIN="$2"; shift 2 ;;
    --no-launchd) LAUNCHD=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || {
  echo "Claude Code Idle Reaper runs on macOS only (it relies on launchd, simctl and lsof)." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required (xcode-select --install)" >&2; exit 1; }

DEST="${DEST/#\~/$HOME}"
DATA="${DATA/#\~/$HOME}"
SETTINGS="${SETTINGS/#\~/$HOME}"
AGENTS="${AGENTS/#\~/$HOME}"
BIN="${BIN/#\~/$HOME}"
if [ -z "$BIN" ]; then
  if [ -d /opt/homebrew/bin ] && [ -w /opt/homebrew/bin ]; then BIN=/opt/homebrew/bin; else BIN="$HOME/.local/bin"; fi
fi
PLIST="$AGENTS/$LABEL.plist"
SCRIPT="$DEST/idle-reaper.py"
SHIM="$DEST/idle-reaper-hook.sh"
DOMAIN="gui/$(id -u)"

# The commands stored in settings.json. $HOME keeps them portable between Macs.
if [ "$DEST" = "$HOME/.claude/hooks" ]; then
  SHIM_CMD='"$HOME/.claude/hooks/idle-reaper-hook.sh"'
  PROMPT_CMD='python3 "$HOME/.claude/hooks/idle-reaper.py" hook >/dev/null 2>&1 || true'
else
  SHIM_CMD="\"$SHIM\""
  PROMPT_CMD="python3 \"$SCRIPT\" hook >/dev/null 2>&1 || true"
fi
EXTRA_ENV=""
if [ "$DATA" != "$HOME/.claude/idle-reaper" ]; then
  SHIM_CMD="IDLE_REAPER_HOME=\"$DATA\" $SHIM_CMD"
  PROMPT_CMD="IDLE_REAPER_HOME=\"$DATA\" $PROMPT_CMD"
  EXTRA_ENV=1
fi

agent_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

unload_agent() {
  [ "$LAUNCHD" = 1 ] || return 0
  if agent_loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do agent_loaded || break; sleep 0.3; done
  fi
}

load_agent() {
  [ "$LAUNCHD" = 1 ] || return 0
  # Right after a bootout launchd can still be letting go of the old job; try a few times.
  for _ in 1 2 3 4 5; do
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && return 0
    sleep 0.5
  done
  echo "could not load $PLIST; try: launchctl bootstrap $DOMAIN \"$PLIST\"" >&2
  return 1
}

if [ "$UNINSTALL" = 0 ]; then
  mkdir -p "$DEST" "$DATA" "$AGENTS" "$BIN"
  cp "$HERE/idle-reaper.py" "$SCRIPT"
  cp "$HERE/idle-reaper-hook.sh" "$SHIM"
  chmod +x "$SCRIPT" "$SHIM"
  if [ ! -f "$DATA/config.json" ]; then
    cp "$HERE/config.example.json" "$DATA/config.json"
  fi

  # The LaunchAgent, with this Mac's paths. The shim calls /usr/bin/python3, so launchd does too.
  if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON="$(command -v python3)"; fi
  DEVELOPER_DIR_PATH="$(xcode-select -p 2>/dev/null || true)"
  [ -n "$DEVELOPER_DIR_PATH" ] || DEVELOPER_DIR_PATH=/Applications/Xcode.app/Contents/Developer
  CHANGED="$(python3 - "$HERE/local.idle-reaper.plist.template" "$PLIST" "$PYTHON" "$SCRIPT" \
    "$DEVELOPER_DIR_PATH" "$DATA" "$EXTRA_ENV" <<'PY'
import os, sys
from xml.sax.saxutils import escape

template, plist, python, script, developer, data, extra = sys.argv[1:8]
with open(template) as fh:
    text = fh.read()
env = ""
if extra:
    env = "\n        <key>IDLE_REAPER_HOME</key>\n        <string>%s</string>" % escape(data)
for key, value in (("{{PYTHON}}", escape(python)), ("{{SCRIPT}}", escape(script)),
                   ("{{DEVELOPER_DIR}}", escape(developer)), ("{{EXTRA_ENV}}", env),
                   ("{{LAUNCHD_LOG}}", escape(os.path.join(data, "launchd.log")))):
    text = text.replace(key, value)
try:
    with open(plist) as fh:
        same = fh.read() == text
except OSError:
    same = False
if not same:
    with open(plist, "w") as fh:
        fh.write(text)
print("same" if same else "changed")
PY
)"
  # A changed plist (or a timer that is not loaded) needs a reload; the script itself does not,
  # since launchd starts it afresh every minute.
  if [ "$LAUNCHD" = 1 ] && { [ "$CHANGED" = changed ] || ! agent_loaded; }; then
    unload_agent
    load_agent || true
  fi

  # `running`: a link to idle-reaper.py. Something else already called `running` is left alone.
  LINK="$BIN/running"
  if [ -L "$LINK" ] && [[ "$(readlink "$LINK")" != *idle-reaper.py ]]; then
    echo "left $LINK alone: it points to $(readlink "$LINK"), not idle-reaper.py" >&2
  elif [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "left $LINK alone: it is not a link to idle-reaper.py. Run $SCRIPT directly instead." >&2
  else
    ln -sfn "$SCRIPT" "$LINK"
  fi
fi

python3 - "$SETTINGS" "$SHIM_CMD" "$PROMPT_CMD" "$UNINSTALL" <<'PY'
import json, os, shutil, sys, time

path, shim_cmd, prompt_cmd, uninstall = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
# event -> (matcher, command, how to recognise ours)
WANTED = {
    "PreToolUse": ("Bash", shim_cmd, "idle-reaper-hook.sh"),
    "PostToolUse": ("Bash", shim_cmd, "idle-reaper-hook.sh"),
    "UserPromptSubmit": (None, prompt_cmd, "idle-reaper.py"),
}

try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    sys.exit("%s is not a JSON object; fix it by hand first" % path)
before = json.dumps(data, sort_keys=True)
hooks = data.setdefault("hooks", {})


def ours(hook, mark=None):
    command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    marks = (mark,) if mark else ("idle-reaper-hook.sh", "idle-reaper.py")
    return any(m in command for m in marks)


if uninstall:
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) if not ours(h)]
            if inner or not group.get("hooks"):
                kept.append(dict(group, hooks=inner) if group.get("hooks") else group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
else:
    for event, (matcher, cmd, mark) in WANTED.items():
        groups = hooks.setdefault(event, [])
        found = False
        for group in groups:
            for hook in group.get("hooks", []):
                if ours(hook, mark):
                    found = True
                    hook["type"] = "command"
                    hook["command"] = cmd
                    hook.setdefault("timeout", 10)
        if found:
            continue
        entry = {"type": "command", "command": cmd, "timeout": 10}
        # Join a group with the same matcher (the usual "Bash" one) rather than add another.
        for group in groups:
            if group.get("matcher") == matcher and isinstance(group.get("hooks"), list):
                group["hooks"].append(entry)
                break
        else:
            group = {"hooks": [entry]}
            if matcher:
                group = {"matcher": matcher, "hooks": [entry]}
            groups.append(group)

if json.dumps(data, sort_keys=True) == before:
    print("settings already up to date: " + path)
else:
    if os.path.exists(path):
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while os.path.exists(backup):  # two runs in one second must not share a backup
            backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S") + "-%d" % n
            n += 1
        shutil.copy(path, backup)
        print("backed up " + backup)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(("removed from " if uninstall else "wired into ") + path)
PY

if [ "$UNINSTALL" = 1 ]; then
  unload_agent
  rm -f "$PLIST"
  echo "removed the timer: $PLIST"
  LINK="$BIN/running"
  if [ -L "$LINK" ] && [[ "$(readlink "$LINK")" == *idle-reaper.py ]]; then
    rm -f "$LINK"
    echo "removed $LINK"
  fi
  if [ "$PURGE" = 1 ]; then
    rm -f "$SCRIPT" "$SHIM"
    for f in config.json state.json state.tmp reaper.log launchd.log .lock; do rm -f "$DATA/$f"; done
    rmdir "$DATA" 2>/dev/null || true
    echo "deleted $SCRIPT, $SHIM, and config, state and logs in $DATA"
  else
    echo "kept $SCRIPT, the shim, and $DATA (add --purge to delete them)"
  fi
  exit 0
fi

if [ "$LAUNCHD" = 1 ]; then
  TIMER="loaded, runs every minute"
else
  TIMER="written, not loaded (--no-launchd)"
fi

cat <<EOF

Installed
  script   $SCRIPT
  shim     $SHIM
  data     $DATA   (config.json, state.json, reaper.log)
  timer    $PLIST   ($TIMER)
  command  $BIN/running

Next
  running                  what is on, and when Claude's things go off
  running set idle 15      give Claude's things 15 quiet minutes instead of 10
  Restart your Claude Code sessions: hooks are read when a session starts.
EOF
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  $BIN is not on your PATH; add it, or run $SCRIPT directly." ;;
esac
