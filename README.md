# Claude Code Idle Reaper

**A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) companion that turns off the iOS simulators and local dev servers Claude started, once they go idle, and never touches the ones you started.** A timer decides, so Claude does not have to remember to clean up, and nothing gets shut down in the middle of the work.

[![tests](https://github.com/TheAbubakrAbu/Claude-Code-Idle-Reaper/actions/workflows/tests.yml/badge.svg)](https://github.com/TheAbubakrAbu/Claude-Code-Idle-Reaper/actions/workflows/tests.yml)

Created by **Abubakr Elmallah** on **November 16, 2026**.

## At a glance

| | |
|---|---|
| **1** script | `idle-reaper.py`, Python 3.8+, standard library only, plus a 10-line hook shim |
| **0** dependencies | nothing to `pip install` |
| **2** kinds of things | iOS simulators and local servers, and only the ones a Claude session started |
| **10 / 30** minutes | the quiet time and the hard limit, each one command to change |
| **1** command | `running`: what is on, who started it, and when Claude's things go off |
| **78** tests | run on macOS on every push |

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [What you'll see](#what-youll-see)
- [Getting Started](#getting-started)
- [Commands](#commands)
  - [Installer](#installer)
  - [The `running` command](#the-running-command)
  - [The hooks](#the-hooks)
- [Settings](#settings)
  - [Environment variables](#environment-variables)
- [How it decides](#how-it-decides)
- [Works with Claude Code Sim Audio](#works-with-claude-code-sim-audio)
- [Teach Claude the other half](#teach-claude-the-other-half)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Privacy & Data](#privacy--data)
- [Testing](#testing)
- [Uninstall](#uninstall)
- [License](#license)
- [Feedback & Contact](#feedback--contact)

---

## Why this exists

**Claude Code starts simulators and dev servers to test its work, and then forgets them.**

A build or a UI test boots a simulator headless, with no window to remind you it is there. A preview starts a dev server in the background. The turn ends, the chat moves on, and they keep running. On one Mac, eight headless simulators that Claude sessions had booted ran for days and overheated it.

The obvious fix, telling Claude to shut everything down at the end of every turn, was no better. The next request needs the same simulator again, so every turn paid for a cold boot, and a server stopped at the end of one turn was started again at the start of the next.

So a timer decides instead. Once a minute it looks at what is on, works out which of it a Claude session started, and turns that off only after its chat has gone quiet. **What you started yourself is never touched.**

## Features

- **Only Claude's things:** a simulator or server counts as Claude's only when there is evidence a Claude session started it. Everything else is listed as yours and left alone.
- **Waits for the chat, not the turn:** a simulator goes off 10 minutes after the chat that booted it goes quiet, so a back-and-forth conversation keeps it warm.
- **Knows when something is in use:** Simulator in front, a UI test running, `xcodebuild` using it, a browser tab connected to a server, a server busy working, or a chat in the middle of a long build all count as in use.
- **A hard limit:** a simulator nothing has touched for 30 minutes goes off even while its chat carries on, and a connected tab holds a server for 30 minutes at most.
- **Tells the chat why:** before it stops a background server it appends a note to that task's output, so Claude reads "this is expected, do not restart it" instead of debugging a crash.
- **Not fooled by restart loops:** a server that a chat restarts without being asked is left on until you next write in that chat, and one stopped twice in a day is left on for the rest of it.
- **Sleep-aware:** after the Mac sleeps, every countdown starts over, so nothing is "idle" for time nobody could use it.
- **One command to see and steer it:** `running` lists what is on in two groups, yours and Claude's, with when each of Claude's goes off; `running keep` pins one, `running pause` stops the timer.
- **Never in Claude's way:** the hooks only leave a note and return; a hook error is logged, never raised.

## What you'll see

`running` prints what is on right now:

```text
Started by you (never touched)
  Simulators
    iPad Pro 11-inch (M5)              on for 2 h 10 min
  Websites and servers
    localhost:5173    Vite dev server        in ~/code/portfolio
    Gradle daemon (Android build helper), not a website

Started by Claude (turned off after 10 quiet minutes)
  Simulators
    iPhone 17 Pro                      Off in 7 min                         my-app chat
  Websites and servers
    localhost:3000    Next.js dev server     In use: a browser tab or client is connected my-app chat
                      in ~/code/my-app

Not shown: 2 editor helpers, 1 Claude sessions. `running --all` lists them.

Turned off lately
    Sun 14:02  port 8080 (python3 -m http.server 8080): its chat has been quiet for 12 min

Timer: Claude's things go off after 10 quiet minutes, 30 at most. Running (last look under a minute ago).
```

Each of Claude's things shows its fate: **Off in 7 min**, **In use: …** (and why), **Kept on for 2 h**, **Left on: …** (and why), or **Timer paused**. Every decision is also written to `~/.claude/idle-reaper/reaper.log`.

## Getting Started

You need macOS and `python3` (the Command Line Tools provide `/usr/bin/python3`; `xcode-select --install`). Xcode is only needed for the simulator half.

1. **Clone the project:**

   ```bash
   git clone https://github.com/TheAbubakrAbu/Claude-Code-Idle-Reaper.git
   cd Claude-Code-Idle-Reaper
   ```

2. **Run the installer:**

   ```bash
   ./install.sh
   ```

   It copies `idle-reaper.py` and `idle-reaper-hook.sh` to `~/.claude/hooks/`, creates `~/.claude/idle-reaper/config.json` from [config.example.json](config.example.json) if there is none, writes and loads the LaunchAgent `~/Library/LaunchAgents/local.idle-reaper.plist` (one pass a minute), links the `running` command into `/opt/homebrew/bin` (or `~/.local/bin` when that is not writable), and wires three hooks into `~/.claude/settings.json`. A backup of `settings.json` is written first, and running it again changes nothing.

3. **Look at what is on:**

   ```bash
   running
   ```

   The last line should say **Running**. Until a Claude session starts something, the "Started by Claude" group says `none`.

4. **Restart your Claude Code sessions.** Hooks are read when a session starts. Run `/hooks` inside Claude Code to see them listed.

5. **Optional: teach Claude the rules.** See [Teach Claude the other half](#teach-claude-the-other-half).

## Commands

### Installer

| Command | Description |
| --- | --- |
| `./install.sh` | Install, wire the hooks, load the timer, and add `running` |
| `./install.sh --dest DIR` | Put `idle-reaper.py` and the shim in `DIR` instead of `~/.claude/hooks` |
| `./install.sh --data DIR` | Keep `config.json`, `state.json` and the log in `DIR` instead of `~/.claude/idle-reaper` (see [Environment variables](#environment-variables)) |
| `./install.sh --settings FILE` | Wire the hooks into another `settings.json` |
| `./install.sh --launch-agents DIR` | Write the LaunchAgent to `DIR` instead of `~/Library/LaunchAgents` |
| `./install.sh --bin DIR` | Link `running` into `DIR` |
| `./install.sh --no-launchd` | Write the LaunchAgent but do not load or unload it |
| `./install.sh --uninstall` | Remove the hooks, the timer and the `running` link, keep the files |
| `./install.sh --uninstall --purge` | Also delete the script, the shim, `config.json`, `state.json` and the logs |

If something else is already called `running` in that folder, the installer leaves it alone and says so.

### The `running` command

| Command | Description |
| --- | --- |
| `running` | What is on, in two groups (started by you, started by Claude), with when each of Claude's goes off |
| `running --all` | The same, plus the background helpers it never touches (editor helpers, Claude itself, macOS services, processes inside a simulator) |
| `running off` | Turn off everything Claude started, now |
| `running off <port\|simulator>` | Turn off one: a port (`3000`), or part of a simulator's name or UDID (`"iPhone 17 Pro"`, `Max`) |
| `running keep <port\|simulator> [hours\|forever]` | Leave this one on: 8 hours unless told otherwise |
| `running release <port\|simulator>` | Undo `keep`: back on the timer |
| `running set idle <minutes>` | Change the quiet time (default 10) |
| `running set limit <minutes>` | Change the hard limit (default 30) |
| `running pause [hours]` | Stop the timer for a while, or until `running resume` |
| `running resume` | Start it again; every countdown starts over |

`running` is a link to `idle-reaper.py`, so `python3 ~/.claude/hooks/idle-reaper.py <command>` does the same. Two more commands are for the machinery, not for you: `tick` (one pass of the timer, what launchd runs every minute) and `hook` (one Claude Code hook event on stdin).

### The hooks

| Event | Command | What it does |
| --- | --- | --- |
| `PreToolUse` (Bash) | `idle-reaper-hook.sh` | Before a simulator command runs, notes which simulators are already on |
| `PostToolUse` (Bash) | `idle-reaper-hook.sh` | After it, notes which simulators it used or booted |
| `UserPromptSubmit` | `idle-reaper.py hook` | Notes that you wrote in that chat (it lifts a "left on until you next write there") |

The shim passes an event on only when the command mentions `simctl`, `xcodebuild`, `Simulator`, `expo`, `flutter`, `react-native`, `maestro` or `fastlane`, so every other command pays for one shell `case` and nothing more.

## Settings

All settings live in **`~/.claude/idle-reaper/config.json`**. Every key is optional; anything you leave out uses its default. Changes take effect on the next pass, within a minute; there is nothing to restart.

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `true` | `false` switches the timer off. `running` still shows everything. |
| `idle_minutes` | `10` | How long after a chat goes quiet its simulators and servers go off. `running set idle <minutes>` changes it. |
| `limit_minutes` | `30` | The longest a simulator stays on while its chat carries on without touching it, and the longest a connected browser tab holds a server. `running set limit <minutes>` changes it. |
| `dry_run` | `false` | `true` logs what it would turn off (once per thing) and turns nothing off. Handy for a first week. |

### Environment variables

| Variable | What it does |
| --- | --- |
| `IDLE_REAPER_HOME` | Use another data folder (default `~/.claude/idle-reaper`). `install.sh --data` sets it in the hooks and the LaunchAgent for you; export it in your shell too, so `running` reads the same folder. |
| `DEVELOPER_DIR` | The Xcode the timer uses for `simctl`. The installer sets it in the LaunchAgent from `xcode-select -p`. |

## How it decides

The rules live in two functions, `sim_deadline` and `server_deadline`, and the evidence comes from three places.

**Who started a server.** Claude Code's Bash tool puts `CLAUDECODE=1`, the session id and its own process id into the environment of everything it runs, and children inherit it even after their parent is gone. A process listening on a TCP port that carries this marker is Claude's, and the whole task that one Bash call started is treated as one server. A server started from a terminal, an editor or Xcode carries none of it and is yours. MCP servers, language servers and Claude's own processes are never touched.

**Who started a simulator.** A simulator carries no such marker, so the hooks leave a note before every simulator command a Claude session runs, with the simulators already on at that moment. A simulator that was off then and came on while the command ran (or within 15 minutes of one sent to the background) was booted by it, provided the command names that simulator or names none at all. A script that boots one without naming a simulator tool in the command Claude typed (`./check.sh`, which runs `xcrun simctl boot`) is caught by the marker on the tools it runs. A simulator with "Claude" in its name is Claude's. Anything nothing explains after three minutes is yours for as long as it stays on.

**When a chat is quiet.** Claude Code writes a chat's transcript (and its subagents') as it works, so the newest write is when that chat last did anything. A chat in the middle of a tool call, such as a long build or test that writes nothing until it ends, counts as active.

**When it goes off.**

- A **simulator** goes off `idle_minutes` after the chat that booted it goes quiet, or `limit_minutes` after anything last touched it while that chat carries on. It stays on while Simulator is the app in front, a UI test runs on it, or `xcodebuild` is using it. It is turned off with `xcrun simctl shutdown`.
- A **server** goes off `idle_minutes` after its chat goes quiet; a connected browser tab or client stretches that to `limit_minutes` at most. One that is working (averaging over a tenth of a processor core) is never stopped, and a new one is watched for four minutes before it can be. It is stopped with `SIGTERM` to the whole task from the top down, then `SIGKILL` for whatever is left six seconds later.
- **After a sleep** (more than five minutes since the last pass), every countdown starts over.
- **`running keep`**, **`running pause`** and `"enabled": false` override all of the above.

**Restart loops.** Stopping a background task wakes the chat that started it, and a chat that knows nothing of this timer may start the server again: stop, wake, restart, a turn each time. So before it stops one, the timer appends a note to the task's output file saying this was expected and not to restart it. A server that comes back with no message from you in between is left on until you next write in that chat, and one already stopped twice in a day is left on for the rest of it.

## Works with Claude Code Sim Audio

[Claude Code Sim Audio](https://github.com/TheAbubakrAbu/Claude-Code-Sim-Audio) keeps simulators off your speakers. By default it mutes every simulator; its optional `claude` mode mutes the ones a Claude session started instead, and it learns which ones those are from this timer's `~/.claude/idle-reaper/state.json`. The two work independently; neither needs the other.

## Teach Claude the other half

The timer cleans up by itself. To stop Claude from cleaning up on its own (shutting a simulator down at the end of every turn, or restarting a server the timer stopped), paste [docs/CLAUDE.md-snippet.md](docs/CLAUDE.md-snippet.md) into your `~/.claude/CLAUDE.md`. It tells Claude to leave things on, reuse what is already running, treat an `[idle-reaper]` note as expected, and pin things with `running keep` when you ask.

## Known Limitations

- **macOS only.** It relies on launchd, `ps`, `lsof`, `lsappinfo` and `simctl`.
- **iOS simulators only.** Anything CoreSimulator runs (iPhone, iPad, Apple Watch, Apple TV, Vision Pro simulators) is covered; Android emulators are not.
- **A server is something listening on a TCP port.** A background job that listens on nothing (a file watcher, a build loop) is never stopped.
- **It only knows what Claude Code's Bash tool started.** Something Claude starts another way (through an MCP server, or an editor task) has no marker and is treated as yours. That is the safe direction: it can leave something on, never turn off something of yours.
- **A process whose whole chain hides its environment is yours.** A server that renames itself writes over the memory `ps` reads its environment from; the timer climbs to the process that started it, but if every process above it is gone or hidden too, it is left alone.
- **Hooks are read when a session starts.** After installing or uninstalling, restart open sessions. Simulators booted before that are judged without the hooks' notes.
- **`running` is a common name.** If another command already has it, the installer leaves that one alone; run `~/.claude/hooks/idle-reaper.py` directly instead.

## Troubleshooting

- **`running` ends with "NOT RUNNING: the background job is not loaded".** Run `./install.sh` again, or load it by hand: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.idle-reaper.plist`.
- **`running: command not found`.** The folder it was linked into is not on your `PATH`; the installer says which one. Add it, or install with `--bin` pointing at a folder that is.
- **Something of Claude's stays on.** `running` says why: **In use: …** names what is using it, **Left on: …** names the rule that is holding it.
- **Something was turned off sooner than I wanted.** Pin it with `running keep <port or name>`, or give everything longer with `running set idle 20`.
- **Something of mine was turned off.** That is a bug. `~/.claude/idle-reaper/reaper.log` has a line saying why it was judged Claude's (for example "a Claude command booted it"); please open an issue with it.
- **I want to watch it before I trust it.** Set `"dry_run": true` in `config.json` and `tail -f ~/.claude/idle-reaper/reaper.log`.
- **I need everything left alone for a while.** `running pause 2` (hours), or `running pause` until `running resume`.

## Privacy & Data

**Claude Code Idle Reaper** never sends anything anywhere; it has no network code at all.

- **What it reads:** the process list (`ps`), and the environment of processes that listen on a port or name a simulator, only to find Claude Code's marker; open TCP connections (`lsof`); the name of the app in front; the modification times of Claude Code transcripts in `~/.claude/projects`, and the first 40 KB of one to find its project folder; simulator names from CoreSimulator's `device.plist` files.
- **What it keeps:** `~/.claude/idle-reaper/state.json` holds what it is watching: session ids, transcript paths, working folders, ports, the first 300 characters of each Claude server's command line, and the first 1,500 characters of each simulator command a Claude session ran (kept for two hours). Sessions it no longer needs are dropped after a week.
- **What it logs:** `~/.claude/idle-reaper/reaper.log` records what it judged Claude's, what it turned off and why. It is trimmed when it passes 500 KB.
- **What it writes elsewhere:** the one-paragraph `[idle-reaper]` note, appended to the output file of a background task it stopped.
- **Your state stays out of git:** `.gitignore` excludes `config.json`, `state.json` and the logs.

## Testing

```bash
python3 -m unittest discover -s tests -v     # 78 tests, nothing real is looked at or touched
```

The tests load a fresh copy of `idle-reaper.py` with its data folder, the transcripts folder and the simulator folder in a temporary directory, and replace `ps`, `lsof`, `lsappinfo`, `launchctl`, `simctl` and `kill` with fakes that only record what they were asked. They cover both deadline rules, who started what, the hooks, the timer turning things off (and leaving yours, busy ones and kept ones on), the commands, and the installer, which runs against a throwaway home folder with `--no-launchd`. A hygiene check keeps em dashes, home-folder paths and signing identities out of the repository. The [GitHub Actions workflow](.github/workflows/tests.yml) runs the suite on macOS on every push.

## Uninstall

```bash
./install.sh --uninstall          # removes the hooks, the timer and `running`, keeps the files
./install.sh --uninstall --purge  # also deletes the script, the shim, config, state and logs
```

Whatever was on stays on; uninstalling only stops the timer.

## License

This project is licensed under the [MIT License](LICENSE). Feel free to use, modify, and distribute the code, but please provide attribution.

## Feedback & Contact

I'd love to hear your thoughts! Open an issue, or reach out with feedback, feature requests, or questions:
- **Email:** contact@abubakrelmallah.com
- **Website:** [abubakrelmallah.com](https://abubakrelmallah.com/)
- **LinkedIn:** [Abubakr Elmallah](https://www.linkedin.com/in/abubakr-elmallah-416a0b273/)
