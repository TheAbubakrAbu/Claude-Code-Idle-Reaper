"""Tests for idle-reaper.py. Nothing real is looked at or touched: every test loads a fresh copy
of the script with IDLE_REAPER_HOME, the transcripts folder and the simulator folder in a
temporary directory, and replaces `ps`, `lsof`, `lsappinfo`, `launchctl`, `simctl` and `kill`
with fakes that only record what they were asked.

    python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import signal
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "idle-reaper.py")

UDID_A = "11111111-2222-3333-4444-555555555555"
UDID_B = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
CFG = {"enabled": True, "idle_minutes": 10, "limit_minutes": 30, "dry_run": False}
LSTART = "%a %b %d %H:%M:%S %Y"


def launchd_sim(udid):
    return ("/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Resources/bin/launchd_sim "
            "/private/sim/Library/Developer/CoreSimulator/Devices/%s/data/var/run/launchd_bootstrap.plist" % udid)


class FakeRun:
    """Stands in for idle-reaper's `run`: answers by the start of the command line, records every call."""

    def __init__(self):
        self.rules, self.calls = [], []

    def answer(self, prefix, output):
        self.rules.append((list(prefix), output))

    def __call__(self, args, timeout=20, env=None):
        self.calls.append(list(args))
        for prefix, output in self.rules:
            if list(args[:len(prefix)]) == prefix:
                return output(args) if callable(output) else output
        return ""


class FakeSubprocess:
    """Stands in for the `subprocess` module inside idle-reaper: launchctl says "not loaded",
    simctl shutdown succeeds, and nothing is ever really run."""

    def __init__(self, loaded=False):
        self.calls, self.loaded = [], loaded
        self.shutdown = (0, "")  # what `simctl shutdown` answers: (exit code, stderr)

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if list(args[:3]) == ["/usr/bin/xcrun", "simctl", "shutdown"]:
            return types.SimpleNamespace(returncode=self.shutdown[0], stdout="", stderr=self.shutdown[1])
        code = 0 if (args[0] != "/bin/launchctl" or self.loaded) else 113
        return types.SimpleNamespace(returncode=code, stdout="", stderr="")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idle-reaper-test-")
        self.data = os.path.join(self.tmp, "data")
        with mock.patch.dict(os.environ, {"IDLE_REAPER_HOME": self.data}):
            spec = importlib.util.spec_from_file_location("idle_reaper_under_test", SCRIPT)
            self.m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.m)
        self.assertTrue(str(self.m.DATA).startswith(self.tmp))  # never the real state
        self.m.PROJECTS = Path(self.tmp) / "projects"
        self.m.DEVICES = Path(self.tmp) / "Devices"
        self.m.PROJECTS.mkdir()
        self.m.DEVICES.mkdir()
        self.fake = FakeRun()
        self.sub = FakeSubprocess()
        self.rows = {}
        self.kills = []
        self.dead = set()
        self.now = int(time.time())
        self.Table = self.m.Table
        patches = [
            mock.patch.object(self.m, "run", self.fake),
            mock.patch.object(self.m, "subprocess", self.sub),
            mock.patch.object(self.m, "Table", self.table),
            mock.patch.object(self.m.os, "kill", self.kill),
            mock.patch.object(self.m.os, "getppid", lambda: 1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # A world of processes ----------------------------------------------------

    def proc(self, pid, ppid, cmd, start, cpu="0:00.00"):
        self.rows[pid] = (ppid, cpu, start, cmd)

    def table(self):
        t = self.Table.__new__(self.Table)
        t.rows, t.kids, t._start = {}, {}, {}
        for pid, (ppid, cpu, start, cmd) in self.rows.items():
            if pid in self.dead:
                continue
            t.rows[pid] = (ppid, cpu, time.strftime(LSTART, time.localtime(start)), cmd)
            t.kids.setdefault(ppid, []).append(pid)
        return t

    def kill(self, pid, sig):
        if pid in self.dead or pid not in self.rows:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        self.kills.append((pid, sig))
        self.dead.add(pid)

    def device(self, udid, name, runtime="com.apple.CoreSimulator.SimRuntime.iOS-27-0"):
        folder = self.m.DEVICES / udid
        folder.mkdir()
        with open(folder / "device.plist", "wb") as fh:
            plistlib.dump({"name": name, "runtime": runtime}, fh)

    def boot(self, udid, name, ago, pid=100):
        self.device(udid, name)
        self.proc(pid, 1, launchd_sim(udid), self.now - ago)
        return self.now - ago

    def claude_sim(self, udid, name, ago_booted, ago_used, sessions=(), pid=100, **extra):
        boot = self.boot(udid, name, ago_booted, pid)
        entry = {"name": name, "boot": boot, "owned_at": self.now - ago_booted,
                 "last_used": self.now - ago_used, "sessions": list(sessions),
                 "why": "a Claude command booted it", "keep_until": 0}
        entry.update(extra)
        return entry

    # State on disk -------------------------------------------------------------

    def state(self, **fields):
        state = self.m.empty_state()
        state["last_tick"] = self.now - 60
        state.update(fields)
        self.m.save_state(state)
        return state

    def saved(self):
        return json.loads(self.m.STATE.read_text())

    def config(self, **fields):
        self.m.DATA.mkdir(parents=True, exist_ok=True)
        self.m.CONFIG.write_text(json.dumps(dict(CFG, **fields)))

    def shutdowns(self):
        return [c for c in self.sub.calls if c[:3] == ["/usr/bin/xcrun", "simctl", "shutdown"]]

    def printed(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def hook(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with mock.patch.object(self.m.sys, "stdin", io.StringIO(raw)):
            self.m.main(["hook"])

    # A server ------------------------------------------------------------------

    def server_world(self, started=3600, marked=True, cpu="0:05.00"):
        """A Claude session (pid 400) with a dev server (pid 500) listening on port 3000."""
        self.proc(400, 1, "claude", self.now - 7200)
        self.proc(500, 400, "node server.js", self.now - started, cpu=cpu)
        self.fake.answer(["/usr/sbin/lsof", "-nP", "-iTCP"], "p500\nn*:3000\nTST=LISTEN\n")
        env = " CLAUDECODE=1 CLAUDE_CODE_SESSION_ID=s1 CLAUDE_PID=400" if marked else ""
        self.fake.answer(["/bin/ps", "eww"], lambda args: "  500 node server.js HOME=/h PATH=/p%s\n" % env)

    def server_entry(self, **fields):
        start = self.now - 3600
        entry = {"pid": 500, "start": start, "ports": [3000], "cmd": "node server.js", "cwd": "/code/app",
                 "session": "s1", "claude_pid": 400, "first_seen": self.now - 3600, "last_connected": 0,
                 "last_busy": 0, "cpu": [[self.now - 300, 5.0]], "keep_until": 0, "exempt": None}
        entry.update(fields)
        return "500:%d" % start, entry

    def idle_server(self, **fields):
        """The server above, on the books and long idle: the next tick stops it unless a rule says no."""
        self.server_world()
        key, entry = self.server_entry(**fields)
        self.state(servers={key: entry}, sessions={"s1": {"seen": self.now - 3600}})
        return key, entry


class Helpers(Base):
    def test_cpu_seconds_reads_ps_time(self):
        self.assertEqual(self.m.cpu_seconds("03:04.50"), 184.5)
        self.assertEqual(self.m.cpu_seconds("1-02:03:04"), 86400 + 2 * 3600 + 3 * 60 + 4)

    def test_span_is_plain_words(self):
        self.assertEqual(self.m.span(30), "under a minute")
        self.assertEqual(self.m.span(25 * 60), "25 min")
        self.assertEqual(self.m.span(3 * 3600), "3 h")
        self.assertEqual(self.m.span(2 * 3600 + 5 * 60), "2 h 5 min")

    def test_names_matches_a_simulator_exactly(self):
        names = self.m.names
        self.assertTrue(names("xcrun simctl boot 'iPhone 17 Pro'", "iPhone 17 Pro"))
        self.assertTrue(names("-destination 'platform=iOS Simulator,name=iPhone 17 Pro'", "iPhone 17 Pro"))
        self.assertFalse(names("xcrun simctl boot 'iPhone 17 Pro Max'", "iPhone 17 Pro"))
        self.assertFalse(names("xcrun simctl boot 'iPhone 17 Pro (Claude)'", "iPhone 17 Pro"))
        self.assertFalse(names("xcrun simctl boot anything", ""))

    def test_command_patterns(self):
        self.assertTrue(self.m.SIMISH.search("xcrun simctl list"))
        self.assertFalse(self.m.SIMISH.search("npm run dev"))
        self.assertFalse(self.m.USES.search("xcrun simctl list devices"))
        self.assertTrue(self.m.USES.search("xcrun simctl launch booted com.example.app"))
        self.assertTrue(self.m.BOOTS.search("xcodebuild test -scheme App"))
        self.assertTrue(self.m.PLUMBING.search("node /x/mcp-server/index.js"))

    def test_config_defaults_and_overrides(self):
        self.assertEqual(self.m.load_config(), self.m.DEFAULTS)
        self.config(idle_minutes=15)
        self.assertEqual(self.m.load_config()["idle_minutes"], 15)
        self.m.CONFIG.write_text("{not json")
        self.assertEqual(self.m.load_config(), self.m.DEFAULTS)

    def test_state_survives_a_corrupt_file(self):
        self.m.DATA.mkdir(parents=True)
        self.m.STATE.write_text("garbage")
        self.assertEqual(self.m.load_state(), self.m.empty_state())

    def test_only_one_writer_holds_the_state_at_a_time(self):
        with self.m.Lock():
            with self.assertRaises(TimeoutError):
                with self.m.Lock(wait=0.2):
                    pass
        with self.m.Lock(wait=0.2):
            pass  # free again once the first one lets go


class SimulatorRules(Base):
    def entry(self, used, owned=0, sessions=("s1",)):
        return {"last_used": used, "owned_at": owned, "sessions": list(sessions)}

    def test_goes_off_idle_minutes_after_last_use_when_the_chat_is_quiet(self):
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 900}
        off, why, since = self.m.sim_deadline(state, self.entry(1000), CFG)
        self.assertEqual((off, since), (1600, 1000))
        self.assertIn("nothing has used it", why)

    def test_goes_off_idle_minutes_after_the_chat_goes_quiet(self):
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 1500}
        off, why, _ = self.m.sim_deadline(state, self.entry(1000), CFG)
        self.assertEqual(off, 2100)
        self.assertIn("its chat has been quiet", why)

    def test_limit_minutes_caps_it_while_the_chat_carries_on(self):
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 3000}
        off, why, _ = self.m.sim_deadline(state, self.entry(1000), CFG)
        self.assertEqual(off, 1000 + 30 * 60)
        self.assertIn("though its chat is still going", why)

    def test_the_floor_restarts_every_countdown(self):
        state = self.m.empty_state()
        state["floor"] = 5000
        state["sessions"]["s1"] = {"seen": 3000}
        self.assertEqual(self.m.sim_deadline(state, self.entry(1000), CFG)[0], 5600)

    def test_a_transcript_being_written_counts_as_the_chat_being_active(self):
        transcript = self.m.PROJECTS / "proj" / "s1.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("{}\n")
        os.utime(transcript, (2000, 2000))
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 100}
        self.assertEqual(self.m.session_activity(state, "s1"), 2000)

    def test_a_chat_in_the_middle_of_a_tool_call_is_busy(self):
        self.proc(400, 1, "claude", self.now - 3600)
        self.proc(401, 400, "/bin/zsh -c source ~/.claude/shell-snapshots/snap.sh && xcodebuild test", self.now - 60)
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 0, "pid": 400, "pid_start": self.now - 3600}
        table = self.table()
        self.assertTrue(self.m.chat_busy(state, "s1", table, self.now))
        self.assertAlmostEqual(self.m.chat_last(state, "s1", table), time.time(), delta=5)

    def test_a_shell_older_than_eleven_minutes_is_a_background_task_not_a_busy_chat(self):
        self.proc(400, 1, "claude", self.now - 3600)
        self.proc(401, 400, "/bin/zsh -c source ~/.claude/shell-snapshots/snap.sh && npm run dev", self.now - 700)
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 0, "pid": 400, "pid_start": self.now - 3600}
        self.assertFalse(self.m.chat_busy(state, "s1", self.table(), self.now))

    def test_a_reused_pid_is_not_the_chat(self):
        self.proc(400, 1, "claude", self.now - 60)
        self.proc(401, 400, "/bin/zsh -c source ~/.claude/shell-snapshots/snap.sh && make", self.now - 30)
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 0, "pid": 400, "pid_start": self.now - 9000}
        self.assertFalse(self.m.chat_busy(state, "s1", self.table(), self.now))


class Attribution(Base):
    def crumb(self, cmd, t, end=None, bg=False, booted=None, session="s1"):
        return {"id": "", "t": t, "end": end, "bg": bg, "cmd": cmd, "booted": booted or {}, "session": session}

    def test_a_command_naming_the_simulator_booted_it(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcrun simctl boot %s" % UDID_A, 1000, end=1010)]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 1005),
                         ("s1", "a Claude command booted it"))

    def test_a_command_that_names_no_simulator_but_boots_one(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcodebuild test -scheme App", 1000, end=1200)]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 1100)[0], "s1")

    def test_a_command_naming_another_simulator_did_not(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcrun simctl boot %s" % UDID_B, 1000, end=1010)]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 1005), (None, None))

    def test_a_simulator_already_on_when_the_command_ran_is_not_its(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcodebuild test", 1000, end=1200, booted={UDID_A: 500})]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 500), (None, None))

    def test_a_simulator_that_came_on_just_before_the_command_is_not_its(self):
        # It booted at 998, inside the window the command covers (which opens five seconds early, at
        # 995), so only the list of what was already on keeps the command from being given it.
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcodebuild test", 1000, end=1200, booted={UDID_A: 998})]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 998), (None, None))

    def test_a_simulator_shut_down_and_booted_again_by_the_command_is_its(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcodebuild test", 1000, end=1200, booted={UDID_A: 500})]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 1100)[0], "s1")

    def test_a_boot_long_after_a_finished_command_is_not_its(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcrun simctl boot %s" % UDID_A, 1000, end=1010)]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 2000), (None, None))

    def test_a_background_command_counts_for_fifteen_minutes(self):
        state = self.m.empty_state()
        state["crumbs"] = [self.crumb("xcodebuild test", 1000, end=1010, bg=True)]
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro", 1800)[0], "s1")

    def test_a_simulator_named_claude_is_claudes(self):
        state = self.m.empty_state()
        self.assertEqual(self.m.attribute(state, UDID_A, "iPhone 17 Pro (Claude tests)", 1000)[1],
                         "its name says a Claude session made it")

    def test_listing_simulators_is_not_using_them(self):
        state = self.m.empty_state()
        state["sims"][UDID_A] = {"name": "iPhone 17 Pro", "sessions": ["s1"], "last_used": 0}
        self.m.touch_sims(state, "s1", "xcrun simctl list devices", 500)
        self.assertEqual(state["sims"][UDID_A]["last_used"], 0)
        self.m.touch_sims(state, "s2", "xcrun simctl launch booted com.example.app", 600)
        self.assertEqual(state["sims"][UDID_A]["last_used"], 600)
        self.m.touch_sims(state, "s3", "xcrun simctl openurl %s https://example.com" % UDID_A, 700)
        self.assertIn("s3", state["sims"][UDID_A]["sessions"])


class ServerRules(Base):
    def entry(self, **fields):
        base = {"session": "s1", "claude_pid": None, "first_seen": 1000, "last_connected": 0, "last_busy": 0}
        base.update(fields)
        return base

    def test_goes_off_idle_minutes_after_its_chat_goes_quiet(self):
        state = self.m.empty_state()
        state["sessions"]["s1"] = {"seen": 900}
        off, why, since = self.m.server_deadline(state, self.entry(), CFG, self.table())
        self.assertEqual((off, since), (1600, 1000))
        self.assertIn("its chat has been quiet", why)

    def test_a_connected_tab_holds_it(self):
        state = self.m.empty_state()
        off, why, _ = self.m.server_deadline(state, self.entry(last_connected=1500), CFG, self.table())
        self.assertEqual(off, 2100)
        self.assertIn("connected tab", why)

    def test_a_connected_tab_holds_it_for_limit_minutes_at_most(self):
        state = self.m.empty_state()
        off, _, _ = self.m.server_deadline(state, self.entry(last_connected=2500), CFG, self.table())
        self.assertEqual(off, 1000 + 30 * 60)

    def test_recent_work_pushes_it_back(self):
        state = self.m.empty_state()
        off, _, _ = self.m.server_deadline(state, self.entry(last_busy=2000), CFG, self.table())
        self.assertEqual(off, 2600)

    def test_an_open_session_that_cannot_be_told_apart_leaves_it_on(self):
        self.proc(400, 1, "claude", self.now - 100)
        off, why, _ = self.m.server_deadline(self.m.empty_state(), self.entry(session=None, claude_pid=400),
                                             CFG, self.table())
        self.assertIsNone(off)
        self.assertIn("cannot be told", why)

    def test_cpu_busy_needs_four_minutes_of_samples(self):
        self.assertIsNone(self.m.cpu_busy([], 100))
        self.assertIsNone(self.m.cpu_busy([[0, 0], [100, 50]], 100))
        self.assertTrue(self.m.cpu_busy([[0, 0], [300, 60]], 300))
        self.assertFalse(self.m.cpu_busy([[0, 0], [300, 10]], 300))

    def test_a_server_restarted_unasked_is_left_on_until_the_next_prompt(self):
        state = self.m.empty_state()
        entry = {"session": "s1", "ports": [3000], "cwd": "/app", "cmd": "npm run dev"}
        self.assertIsNone(self.m.restarted_unasked(state, entry, 5000))
        state["tombstones"] = [{"t": 4000, "session": "s1", "ports": [3000], "cwd": "/app", "cmd": "npm run dev"}]
        self.assertTrue(self.m.restarted_unasked(state, entry, 5000)["until_prompt"])
        state["sessions"]["s1"] = {"prompt": 4500}
        self.assertIsNone(self.m.restarted_unasked(state, entry, 5000))

    def test_a_server_stopped_twice_today_is_left_on_for_a_day(self):
        state = self.m.empty_state()
        entry = {"session": "s1", "ports": [3000]}
        state["tombstones"] = [{"t": 3000, "session": "s1", "ports": [3000]},
                               {"t": 4000, "session": "s1", "ports": [3000]}]
        self.assertEqual(self.m.restarted_unasked(state, entry, 5000)["until"], 5000 + 86400)


class Hooks(Base):
    def test_a_typed_prompt_is_noted_and_lifts_an_until_prompt_exemption(self):
        self.state(servers={
            "1:1": {"session": "s1", "exempt": {"why": "x", "until_prompt": True}},
            "2:2": {"session": "s2", "exempt": {"why": "x", "until_prompt": True}}})
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "run the app",
                   "cwd": "/code/app"})
        state = self.saved()
        self.assertGreaterEqual(state["sessions"]["s1"]["prompt"], self.now)
        self.assertEqual(state["sessions"]["s1"]["cwd"], "/code/app")
        self.assertIsNone(state["servers"]["1:1"]["exempt"])
        self.assertTrue(state["servers"]["2:2"]["exempt"]["until_prompt"])

    def test_a_prompt_the_harness_wrote_is_not_the_user_writing(self):
        for prompt in ("<task-notification>done</task-notification>", "<system-reminder>x</system-reminder>"):
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": prompt})
        self.assertFalse(self.m.STATE.exists())

    def test_commands_that_cannot_involve_a_simulator_are_ignored(self):
        self.hook({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                   "tool_input": {"command": "npm test"}})
        self.hook({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read",
                   "tool_input": {"command": "xcrun simctl boot x"}})
        self.assertFalse(self.m.STATE.exists())

    def test_a_simulator_command_leaves_a_crumb_and_its_end_closes_it(self):
        self.boot(UDID_B, "iPad Pro", 600, pid=200)
        base = {"session_id": "s1", "tool_name": "Bash", "tool_use_id": "t1",
                "tool_input": {"command": "xcrun simctl boot %s" % UDID_A}}
        self.hook(dict(base, hook_event_name="PreToolUse"))
        crumb = self.saved()["crumbs"][-1]
        self.assertEqual((crumb["session"], crumb["end"], crumb["bg"]), ("s1", None, False))
        self.assertEqual(crumb["booted"], {UDID_B: self.now - 600})
        self.hook(dict(base, hook_event_name="PostToolUse"))
        self.assertIsNotNone(self.saved()["crumbs"][-1]["end"])

    def test_background_commands_are_marked_as_such(self):
        for i, (cmd, bg) in enumerate((("xcodebuild test", True), ("xcodebuild test &", False),
                                       ("nohup xcodebuild test", False))):
            self.hook({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                       "tool_use_id": "t%d" % i, "tool_input": {"command": cmd, "run_in_background": bg}})
        self.assertEqual([c["bg"] for c in self.saved()["crumbs"]], [True, True, True])

    def test_a_broken_payload_never_raises(self):
        self.hook("not json")
        self.assertFalse(self.m.STATE.exists())

    def test_a_hook_error_is_logged_not_raised(self):
        with mock.patch.object(self.m, "Table", side_effect=RuntimeError("boom")):
            self.hook({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                       "tool_input": {"command": "xcrun simctl boot x"}})
        self.assertIn("hook error", self.m.LOG.read_text())


class Timer(Base):
    def test_turns_off_an_idle_simulator_claude_booted(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [["/usr/bin/xcrun", "simctl", "shutdown", UDID_A]])
        state = self.saved()
        self.assertNotIn(UDID_A, state["sims"])
        self.assertEqual(state["history"][-1]["what"], "iPhone 17 Pro")
        self.assertIn("turned off iPhone 17 Pro", self.m.LOG.read_text())

    def test_never_touches_a_simulator_the_user_booted(self):
        self.boot(UDID_A, "iPhone 17 Pro", 3600)
        self.state()
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertIn(UDID_A, self.saved()["left_alone"])

    def test_leaves_a_recently_used_simulator_on(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 120)})
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])

    def test_leaves_a_simulator_on_while_simulator_is_in_front(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.fake.answer(["/usr/bin/lsappinfo", "front"], "ASN:0x0-0x1:\n")
        self.fake.answer(["/usr/bin/lsappinfo", "info"], '"Simulator" ASN:0x0-0x1: (in front)\n')
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertEqual(self.saved()["sims"][UDID_A]["doing"], "you have Simulator in front")

    def test_leaves_a_simulator_on_while_a_test_runs_on_it(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.proc(101, 100, "/x/Devices/%s/data/App-Runner.app/App-Runner" % UDID_A, self.now - 30)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])

    def test_a_kept_simulator_stays_on(self):
        sim = self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200, keep_until=self.now + 3600)
        self.state(sims={UDID_A: sim})
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])

    def test_nothing_goes_off_while_paused_or_switched_off(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)},
                   paused_until=self.now + 3600)
        self.m.main(["tick"])
        self.config(enabled=False)
        state = self.saved()
        state["paused_until"] = 0
        self.m.save_state(state)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])

    def test_a_dry_run_only_logs_once(self):
        self.config(dry_run=True)
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.m.main(["tick"])
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertEqual(self.m.LOG.read_text().count("dry run, would turn off iPhone 17 Pro"), 1)

    def test_after_sleep_every_countdown_starts_over(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)},
                   last_tick=self.now - 3000)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertIn("every timer starts over", self.m.LOG.read_text())

    def test_leaves_a_simulator_on_while_xcodebuild_names_it(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.proc(600, 1, "xcodebuild build -scheme App -destination id=%s" % UDID_A, self.now - 30)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertEqual(self.saved()["sims"][UDID_A]["doing"], "xcodebuild is using it")

    def test_leaves_a_simulator_on_while_an_xcodebuild_test_names_none(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.proc(600, 1, "xcodebuild test -scheme App", self.now - 30)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [])
        self.assertEqual(self.saved()["sims"][UDID_A]["doing"], "xcodebuild is running")

    def test_an_xcodebuild_for_the_mac_or_another_simulator_does_not_hold_it(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.proc(600, 1, "xcodebuild test -scheme Mac -destination platform=macOS", self.now - 30)
        self.proc(601, 1, "xcodebuild test -scheme App -destination id=%s" % UDID_B, self.now - 30)
        self.m.main(["tick"])
        self.assertEqual(self.shutdowns(), [["/usr/bin/xcrun", "simctl", "shutdown", UDID_A]])

    def test_a_simulator_that_would_not_shut_down_stays_on_the_books(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.sub.shutdown = (1, "An error was encountered processing the command")
        self.m.main(["tick"])
        state = self.saved()
        self.assertIn(UDID_A, state["sims"])
        self.assertEqual(state["history"], [])
        self.assertIn("could NOT turn off iPhone 17 Pro", self.m.LOG.read_text())

    def test_a_simulator_that_was_already_off_counts_as_turned_off(self):
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 1200)})
        self.sub.shutdown = (149, "Unable to shutdown device in current state: Shutdown")
        self.m.main(["tick"])
        self.assertNotIn(UDID_A, self.saved()["sims"])

    def test_a_simulator_that_just_came_on_is_not_judged_yet(self):
        self.boot(UDID_A, "iPhone 17 Pro", 60)
        self.state()
        self.m.main(["tick"])
        state = self.saved()
        self.assertNotIn(UDID_A, state["left_alone"])  # the script that booted it may name it yet
        self.assertNotIn(UDID_A, state["sims"])

    def test_a_simulator_of_yours_that_claude_boots_afresh_is_judged_again(self):
        boot = self.boot(UDID_A, "iPhone 17 Pro", 600)
        crumb = {"id": "", "t": boot - 5, "end": boot + 10, "bg": False, "session": "s1", "booted": {},
                 "cmd": "xcrun simctl boot %s" % UDID_A}
        self.state(left_alone={UDID_A: self.now - 9000}, crumbs=[crumb])  # yours, the last time it was on
        self.m.main(["tick"])
        state = self.saved()
        self.assertNotIn(UDID_A, state["left_alone"])
        self.assertEqual(state["sims"][UDID_A]["sessions"], ["s1"])

    # Simulators a chat's own script booted -------------------------------------

    def script_world(self, booted_ago, command_ago, tool="xcrun simctl bootstatus %s -b" % UDID_A):
        """A chat's Bash call (pid 700) running a script, whose tool (pid 701) names a simulator.
        The hook left no crumb: the command the chat typed (`./check.sh`) names no simulator tool."""
        self.boot(UDID_A, "iPhone 17 Pro", booted_ago)
        self.proc(400, 1, "claude", self.now - 7200)
        shell = "/bin/zsh -c source ~/.claude/shell-snapshots/snap.sh && ./check.sh"
        self.proc(700, 400, shell, self.now - command_ago)
        self.proc(701, 700, tool, self.now - command_ago + 5)
        mark = " HOME=/h PATH=/p CLAUDECODE=1 CLAUDE_CODE_SESSION_ID=s9 CLAUDE_PID=400"
        self.fake.answer(["/bin/ps", "eww"], "  700 %s%s\n  701 %s%s\n" % (shell, mark, tool, mark))
        self.state()
        self.m.main(["tick"])
        return self.saved()

    def test_a_simulator_booted_by_a_script_the_chat_ran_is_claudes(self):
        state = self.script_world(booted_ago=60, command_ago=120)
        sim = state["sims"][UDID_A]
        self.assertEqual((sim["why"], sim["sessions"]), ("a script a Claude command ran booted it", ["s9"]))
        self.assertEqual(self.shutdowns(), [])

    def test_a_simulator_you_booted_that_a_chat_reaches_for_later_stays_yours(self):
        state = self.script_world(booted_ago=600, command_ago=120)
        self.assertNotIn(UDID_A, state["sims"])
        self.assertIn(UDID_A, state["left_alone"])

    def test_a_script_naming_the_same_model_on_another_ios_does_not_mean_this_one(self):
        named = "xcodebuild test -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=%s'"
        self.assertNotIn(UDID_A, self.script_world(60, 120, tool=named % "26.0")["sims"])  # this one runs 27.0

    def test_a_script_naming_the_model_and_its_ios_means_this_one(self):
        named = "xcodebuild test -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=%s'"
        self.assertEqual(self.script_world(60, 120, tool=named % "27.0")["sims"][UDID_A]["sessions"], ["s9"])

    # Servers -------------------------------------------------------------------

    def test_never_touches_a_server_the_user_started(self):
        self.server_world(marked=False)
        self.state()
        self.m.main(["tick"])
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.saved()["servers"], {})

    def test_a_new_claude_server_is_watched_not_stopped(self):
        self.server_world(started=60)
        self.state(sessions={"s1": {"seen": self.now - 3600}})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])
        entry = list(self.saved()["servers"].values())[0]
        self.assertEqual((entry["session"], entry["ports"]), ("s1", [3000]))

    def test_stops_an_idle_claude_server_and_tells_its_chat_why(self):
        self.server_world()
        output = os.path.join(self.tmp, "tasks", "b1.output")
        os.makedirs(os.path.dirname(output))
        Path(output).write_text("listening on 3000\n")
        self.fake.answer(["/usr/sbin/lsof", "-nP", "-a"], "p500\nf1\ntREG\nn%s\n" % output)
        key, entry = self.server_entry()
        self.state(servers={key: entry}, sessions={"s1": {"seen": self.now - 3600}})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [(500, signal.SIGTERM)])
        self.assertIn("[idle-reaper] Stopped after 10 quiet minutes", Path(output).read_text())
        state = self.saved()
        self.assertEqual(state["servers"], {})
        self.assertEqual(state["tombstones"][-1]["ports"], [3000])

    def test_a_connected_browser_tab_keeps_a_server_on(self):
        self.server_world()
        self.fake.rules = [r for r in self.fake.rules if r[0][:3] != ["/usr/sbin/lsof", "-nP", "-iTCP"]]
        self.fake.answer(["/usr/sbin/lsof", "-nP", "-iTCP"],
                         "p500\nn*:3000\nTST=LISTEN\nn127.0.0.1:3000->127.0.0.1:51000\nTST=ESTABLISHED\n")
        key, entry = self.server_entry()
        self.state(servers={key: entry}, sessions={"s1": {"seen": self.now - 3600}})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.saved()["servers"][key]["doing"], "a browser tab or client is connected")

    def test_a_busy_server_is_never_stopped(self):
        self.server_world(cpu="2:00.00")
        key, entry = self.server_entry()
        self.state(servers={key: entry}, sessions={"s1": {"seen": self.now - 3600}})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])

    def test_a_kept_server_stays_on(self):
        self.idle_server(keep_until=self.now + 3600)
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])

    def test_a_server_restarted_unasked_is_left_on(self):
        self.idle_server(exempt={"why": "left on until you next write there", "until_prompt": True})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])

    def test_a_day_long_exemption_runs_out(self):
        self.idle_server(exempt={"why": "stopped twice today already, so left on", "until": self.now - 60})
        self.m.main(["tick"])
        self.assertEqual(self.kills, [(500, signal.SIGTERM)])

    def test_a_server_is_not_stopped_before_four_minutes_of_measurements(self):
        self.idle_server(cpu=[[self.now - 60, 5.0]])  # idle on paper, but too new to know if it is working
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])

    def test_a_server_that_renamed_itself_keeps_its_chats_marker(self):
        # next-server writes its new title over the memory `ps` reads the environment from, so the
        # listening process shows no marker (and no HOME or PATH); the `node` that started it still does.
        self.proc(400, 1, "claude", self.now - 7200)
        self.proc(500, 400, "node node_modules/.bin/next dev", self.now - 3600)
        self.proc(501, 500, "next-server (v16.1.1)", self.now - 3590)
        self.fake.answer(["/usr/sbin/lsof", "-nP", "-iTCP"], "p501\nn*:3000\nTST=LISTEN\n")
        self.fake.answer(["/bin/ps", "eww"], "  500 node node_modules/.bin/next dev HOME=/h PATH=/p CLAUDECODE=1 "
                                             "CLAUDE_CODE_SESSION_ID=s1 CLAUDE_PID=400\n  501 next-server (v16.1.1)\n")
        self.state()
        self.m.main(["tick"])
        servers = list(self.saved()["servers"].values())
        self.assertEqual([(s["session"], s["pid"], s["ports"]) for s in servers], [("s1", 500, [3000])])

    # What must never be killed ---------------------------------------------------

    def test_a_server_holding_a_claude_session_is_in_use_and_never_killed(self):
        key, entry = self.idle_server()
        self.proc(501, 500, "claude", self.now - 100)  # a Claude session started from inside the task
        self.m.main(["tick"])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.saved()["servers"][key]["doing"], "a Claude session runs inside it")
        # Asked to directly, the stop itself still refuses.
        self.assertEqual(self.m.stop_server(entry, 10), (False, "a Claude session runs inside it"))
        self.assertEqual(self.kills, [])

    def test_a_reused_pid_is_never_killed(self):
        self.proc(500, 1, "postgres -D /data", self.now - 10)  # someone else's process with the old number
        _, entry = self.server_entry()
        self.assertEqual(self.m.stop_server(entry, 10), (True, "already gone"))
        self.assertEqual(self.kills, [])

    def marked_listener(self, cmd):
        """A listening process that carries a chat's marker, as everything a Bash call starts does."""
        self.server_world()
        self.proc(500, 400, cmd, self.now - 3600)
        self.state(sessions={"s1": {"seen": self.now - 3600}})
        self.m.main(["tick"])
        self.m.main(["tick"])
        return self.saved()

    def test_an_mcp_server_is_never_treated_as_a_server_to_stop(self):
        state = self.marked_listener("node /x/mcp-server/index.js")
        self.assertEqual((state["servers"], self.kills), ({}, []))
        self.assertEqual(len(state["not_claude"]), 1)

    def test_a_claude_session_that_listens_is_never_treated_as_a_server_to_stop(self):
        state = self.marked_listener("claude")
        self.assertEqual((state["servers"], self.kills), ({}, []))

    def test_a_server_that_ignores_the_polite_stop_gets_a_hard_one(self):
        self.server_world()
        _, entry = self.server_entry()
        clock = [float(self.now)]

        def stubborn(pid, sig):  # dies only to SIGKILL
            if pid in self.dead or pid not in self.rows:
                raise ProcessLookupError(pid)
            if sig:
                self.kills.append((pid, sig))
            if sig == signal.SIGKILL:
                self.dead.add(pid)

        def nap(seconds):  # the six seconds it is given pass at once
            clock[0] += seconds

        with mock.patch.object(self.m.os, "kill", stubborn), \
                mock.patch.object(self.m.time, "time", lambda: clock[0]), \
                mock.patch.object(self.m.time, "sleep", nap):
            self.assertEqual(self.m.stop_server(entry, 10), (True, "needed a hard stop"))
        self.assertEqual(self.kills, [(500, signal.SIGTERM), (500, signal.SIGKILL)])


class Commands(Base):
    def two_sims(self):
        a = self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 60, pid=100)
        b = self.claude_sim(UDID_B, "iPhone 17 Pro Max", 3600, 60, pid=200)
        self.state(sims={UDID_A: a, UDID_B: b})

    def test_keep_release_and_forever(self):
        self.two_sims()
        out = self.printed(self.m.main, ["keep", "iPhone 17 Pro", "2"])
        self.assertIn("iPhone 17 Pro: kept on for 2 h", out)
        self.assertAlmostEqual(self.saved()["sims"][UDID_A]["keep_until"], self.now + 7200, delta=5)
        self.printed(self.m.main, ["keep", "Max", "forever"])
        self.assertEqual(self.saved()["sims"][UDID_B]["keep_until"], self.m.FOREVER)
        self.assertIn("back on the timer", self.printed(self.m.main, ["release", "Max"]))
        self.assertEqual(self.saved()["sims"][UDID_B]["keep_until"], 0)

    def test_keep_wants_one_match(self):
        self.two_sims()
        self.assertIn("matches 2 things", self.printed(self.m.main, ["keep", "iphone"]))
        self.assertIn("No single match", self.printed(self.m.main, ["keep", "pixel"]))

    def test_off_turns_off_only_what_claude_started(self):
        self.boot("CCCCCCCC-DDDD-EEEE-FFFF-000000000000", "iPad Air", 9000, pid=300)
        self.two_sims()
        out = self.printed(self.m.main, ["off"])
        self.assertIn("Turned off iPhone 17 Pro\n", out)
        self.assertEqual(sorted(c[3] for c in self.shutdowns()), sorted([UDID_A, UDID_B]))  # not the iPad

    def test_off_one(self):
        self.two_sims()
        self.printed(self.m.main, ["off", "Max"])
        self.assertEqual(self.shutdowns(), [["/usr/bin/xcrun", "simctl", "shutdown", UDID_B]])

    def test_keep_and_off_take_a_port(self):
        key, _ = self.idle_server()
        self.assertIn("port 3000: kept on for 8 h", self.printed(self.m.main, ["keep", "3000"]))
        self.assertAlmostEqual(self.saved()["servers"][key]["keep_until"], self.now + 8 * 3600, delta=5)
        self.m.main(["tick"])
        self.assertEqual(self.kills, [], "kept, so the timer leaves it")
        self.assertIn("Turned off port 3000", self.printed(self.m.main, ["off", "3000"]))
        self.assertEqual(self.kills, [(500, signal.SIGTERM)])
        state = self.saved()
        self.assertEqual(state["servers"], {})
        self.assertEqual(state["tombstones"], [], "one you turned off yourself may be started again freely")

    def test_off_with_nothing_on(self):
        self.state()
        self.assertIn("Nothing Claude started is on.", self.printed(self.m.main, ["off"]))

    def test_set_changes_one_timer_and_keeps_the_rest(self):
        self.config(dry_run=True)
        self.assertIn("Quiet time is now 15 minutes.", self.printed(self.m.main, ["set", "idle", "15"]))
        self.printed(self.m.main, ["set", "limit", "45"])
        cfg = json.loads(self.m.CONFIG.read_text())
        self.assertEqual((cfg["idle_minutes"], cfg["limit_minutes"], cfg["dry_run"]), (15, 45, True))
        self.assertIn("status", self.printed(self.m.main, ["set", "idle", "soon"]))  # usage, nothing written
        self.assertEqual(json.loads(self.m.CONFIG.read_text())["idle_minutes"], 15)

    def test_pause_and_resume(self):
        self.state()
        self.assertIn("paused for 2 hours", self.printed(self.m.main, ["pause", "2"]))
        self.assertAlmostEqual(self.saved()["paused_until"], self.now + 7200, delta=5)
        self.printed(self.m.main, ["pause"])
        self.assertEqual(self.saved()["paused_until"], self.m.FOREVER)
        self.assertIn("running again", self.printed(self.m.main, ["resume"]))
        state = self.saved()
        self.assertEqual(state["paused_until"], 0)
        self.assertGreaterEqual(state["floor"], self.now)

    def test_status_sorts_yours_from_claudes(self):
        self.boot("CCCCCCCC-DDDD-EEEE-FFFF-000000000000", "iPad Air", 9000, pid=300)
        self.state(sims={UDID_A: self.claude_sim(UDID_A, "iPhone 17 Pro", 3600, 60, pid=100)},
                   left_alone={"CCCCCCCC-DDDD-EEEE-FFFF-000000000000": self.now - 9000})
        out = self.printed(self.m.main, [])
        yours, claudes = out.split("Started by Claude")
        self.assertIn("Started by you (never touched)", yours)
        self.assertIn("iPad Air", yours)
        self.assertIn("iPhone 17 Pro", claudes)
        self.assertRegex(claudes, r"Off in [89] min")
        self.assertIn("NOT RUNNING: the background job is not loaded.", out)

    def test_status_says_when_the_timer_runs(self):
        self.sub.loaded = True
        self.state()
        self.assertIn("Running (last look", self.printed(self.m.main, ["status"]))

    def test_unknown_commands_print_the_usage(self):
        out = self.printed(self.m.main, ["help"])
        for command in ("status", "off [port|simulator]", "keep <port|simulator>", "release", "set idle|limit",
                        "pause [hours]", "tick", "hook"):
            self.assertIn(command, out)


class Shim(unittest.TestCase):
    """idle-reaper-hook.sh hands only simulator commands to the idle-reaper.py next to it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idle-reaper-shim-")
        shutil.copy(os.path.join(ROOT, "idle-reaper-hook.sh"), self.tmp)
        self.got = os.path.join(self.tmp, "got.txt")
        with open(os.path.join(self.tmp, "idle-reaper.py"), "w") as fh:
            fh.write("import sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '|' + sys.stdin.read() + '\\n')\n"
                     % self.got)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def send(self, command):
        import subprocess
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        done = subprocess.run([os.path.join(self.tmp, "idle-reaper-hook.sh")], input=payload, text=True,
                              capture_output=True, timeout=20)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        return payload

    def test_passes_simulator_commands_on(self):
        payload = self.send("xcrun simctl boot 'iPhone 17 Pro'")
        with open(self.got) as fh:
            self.assertEqual(fh.read(), "hook|" + payload + "\n")

    def test_passes_every_kind_of_simulator_command_on(self):
        for command in ("xcodebuild test -scheme App", "open -a Simulator", "npx expo start", "flutter run",
                        "npx react-native run-ios", "maestro test flow.yaml", "fastlane scan"):
            with self.subTest(command=command):
                if os.path.exists(self.got):
                    os.remove(self.got)
                payload = self.send(command)
                with open(self.got) as fh:
                    self.assertEqual(fh.read(), "hook|" + payload + "\n")

    def test_skips_everything_else(self):
        self.send("npm test")
        self.assertFalse(os.path.exists(self.got))


if __name__ == "__main__":
    unittest.main()
