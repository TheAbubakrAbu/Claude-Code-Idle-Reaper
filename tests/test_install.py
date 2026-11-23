"""Tests for install.sh. Every run gets a throwaway HOME, `--bin` in that HOME and `--no-launchd`,
so the real settings.json, LaunchAgents folder, /opt/homebrew/bin and launchd are never touched.

    python3 -m unittest discover -s tests -v
"""
import glob
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")
SHIM_CMD = '"$HOME/.claude/hooks/idle-reaper-hook.sh"'
PROMPT_CMD = 'python3 "$HOME/.claude/hooks/idle-reaper.py" hook >/dev/null 2>&1 || true'


@unittest.skipUnless(sys.platform == "darwin", "the installer is macOS only")
class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idle-reaper-install-")
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.home)
        self.claude = os.path.join(self.home, ".claude")
        self.settings = os.path.join(self.claude, "settings.json")
        self.hooks = os.path.join(self.claude, "hooks")
        self.data = os.path.join(self.claude, "idle-reaper")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", "local.idle-reaper.plist")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, *args, check=True):
        env = dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run(["bash", INSTALL, "--bin", self.bin, "--no-launchd"] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)
        if check:
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def read_settings(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def write_settings(self, data):
        os.makedirs(self.claude, exist_ok=True)
        with open(self.settings, "w") as fh:
            json.dump(data, fh, indent=2)

    def commands(self, event):
        return [h["command"] for g in self.read_settings()["hooks"].get(event, []) for h in g["hooks"]]

    def backups(self):
        return glob.glob(self.settings + ".bak-*")

    def test_installs_everything_where_the_live_setup_has_it(self):
        self.install()
        for name in ("idle-reaper.py", "idle-reaper-hook.sh"):
            path = os.path.join(self.hooks, name)
            self.assertTrue(os.access(path, os.X_OK), path)
        with open(os.path.join(ROOT, "idle-reaper.py")) as a, open(os.path.join(self.hooks, "idle-reaper.py")) as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(self.data, "config.json")) as fh:
            self.assertEqual(json.load(fh), {"enabled": True, "idle_minutes": 10, "limit_minutes": 30,
                                             "dry_run": False})
        self.assertEqual(os.readlink(os.path.join(self.bin, "running")), os.path.join(self.hooks, "idle-reaper.py"))

    def test_wires_the_three_hooks_with_home_relative_commands(self):
        self.install()
        hooks = self.read_settings()["hooks"]
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": SHIM_CMD, "timeout": 10}]}])
        self.assertEqual(hooks["PostToolUse"], hooks["PreToolUse"])
        self.assertEqual(hooks["UserPromptSubmit"], [{"hooks": [
            {"type": "command", "command": PROMPT_CMD, "timeout": 10}]}])

    def test_writes_a_valid_launch_agent_with_this_macs_paths(self):
        self.install()
        with open(self.plist, "rb") as fh:
            agent = plistlib.load(fh)
        self.assertEqual(agent["Label"], "local.idle-reaper")
        self.assertEqual(agent["ProgramArguments"][1:], [os.path.join(self.hooks, "idle-reaper.py"), "tick"])
        self.assertTrue(os.access(agent["ProgramArguments"][0], os.X_OK))
        self.assertEqual(agent["StartInterval"], 60)
        self.assertIn("DEVELOPER_DIR", agent["EnvironmentVariables"])
        self.assertNotIn("IDLE_REAPER_HOME", agent["EnvironmentVariables"])
        self.assertEqual(agent["StandardOutPath"], os.path.join(self.data, "launchd.log"))

    def test_running_it_again_changes_nothing(self):
        self.install()
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            json.dump({"idle_minutes": 25}, fh)  # the user's own setting
        with open(self.plist) as fh:
            plist = fh.read()
        before = self.read_settings()
        done = self.install()
        self.assertIn("settings already up to date", done.stdout)
        self.assertEqual(self.read_settings(), before)
        self.assertEqual(self.backups(), [])
        with open(self.plist) as fh:
            self.assertEqual(fh.read(), plist)
        with open(os.path.join(self.data, "config.json")) as fh:
            self.assertEqual(json.load(fh), {"idle_minutes": 25})

    def test_joins_existing_hooks_without_disturbing_them(self):
        guard = {"type": "command", "command": 'python3 "$HOME/.claude/hooks/git-guard.py"', "timeout": 10}
        notify = {"hooks": [{"type": "command", "command": 'python3 "$HOME/.claude/hooks/notify.py"'}]}
        self.write_settings({"model": "opus", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [guard]}], "Stop": [notify]}})
        self.install()
        data = self.read_settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["Stop"], [notify])
        self.assertEqual(data["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [
            guard, {"type": "command", "command": SHIM_CMD, "timeout": 10}]}])
        self.assertEqual(len(self.backups()), 1)

    def test_uninstall_removes_the_wiring_and_keeps_the_files(self):
        guard = {"type": "command", "command": 'python3 "$HOME/.claude/hooks/git-guard.py"', "timeout": 10}
        self.write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [guard]}]}})
        self.install()
        self.install("--uninstall")
        self.assertEqual(self.read_settings(), {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [guard]}]}})
        self.assertFalse(os.path.exists(self.plist))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "running")))
        self.assertTrue(os.path.exists(os.path.join(self.hooks, "idle-reaper.py")))
        self.assertTrue(os.path.exists(os.path.join(self.data, "config.json")))

    def test_uninstall_purge_deletes_only_what_it_made(self):
        self.install()
        for name in ("state.json", "reaper.log", ".lock"):
            open(os.path.join(self.data, name), "w").close()
        with open(os.path.join(self.data, "notes.txt"), "w") as fh:
            fh.write("mine")
        self.install("--uninstall", "--purge")
        self.assertNotIn("hooks", self.read_settings())
        self.assertFalse(os.path.exists(os.path.join(self.hooks, "idle-reaper.py")))
        self.assertFalse(os.path.exists(os.path.join(self.hooks, "idle-reaper-hook.sh")))
        self.assertEqual(os.listdir(self.data), ["notes.txt"])

    def test_custom_places_use_full_paths_and_carry_the_data_folder(self):
        dest, data = os.path.join(self.tmp, "my hooks"), os.path.join(self.tmp, "my data")
        settings, agents = os.path.join(self.tmp, "settings.json"), os.path.join(self.tmp, "agents")
        self.install("--dest", dest, "--data", data, "--settings", settings, "--launch-agents", agents)
        with open(settings) as fh:
            hooks = json.load(fh)["hooks"]
        shim = hooks["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(shim, 'IDLE_REAPER_HOME="%s" "%s/idle-reaper-hook.sh"' % (data, dest))
        self.assertTrue(hooks["UserPromptSubmit"][0]["hooks"][0]["command"].startswith('IDLE_REAPER_HOME="%s" python3 "%s/' % (data, dest)))
        with open(os.path.join(agents, "local.idle-reaper.plist"), "rb") as fh:
            agent = plistlib.load(fh)
        self.assertEqual(agent["EnvironmentVariables"]["IDLE_REAPER_HOME"], data)
        self.assertEqual(agent["ProgramArguments"][1], os.path.join(dest, "idle-reaper.py"))

    def test_the_wired_hook_command_reaches_the_script(self):
        dest, data = os.path.join(self.tmp, "hooks"), os.path.join(self.tmp, "data")
        settings = os.path.join(self.tmp, "settings.json")
        self.install("--dest", dest, "--data", data, "--settings", settings)
        with open(settings) as fh:
            command = json.load(fh)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s-test", "prompt": "hi"})
        subprocess.run(["/bin/sh", "-c", command], input=payload, text=True, timeout=30,
                       env=dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1"))
        with open(os.path.join(data, "state.json")) as fh:
            self.assertIn("s-test", json.load(fh)["sessions"])

    def test_leaves_someone_elses_running_command_alone(self):
        os.makedirs(self.bin)
        other = os.path.join(self.bin, "running")
        with open(other, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")
        done = self.install()
        self.assertIn("left", done.stderr)
        with open(other) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\necho mine\n")
        self.install("--uninstall")
        self.assertTrue(os.path.exists(other), "uninstall removes only its own link")

    def test_leaves_a_running_link_to_something_else_alone(self):
        os.makedirs(self.bin)
        link, other = os.path.join(self.bin, "running"), os.path.join(self.tmp, "other-tool")
        open(other, "w").close()
        os.symlink(other, link)
        done = self.install()
        self.assertIn("left %s alone" % link, done.stderr)
        self.assertEqual(os.readlink(link), other)
        self.install("--uninstall")
        self.assertEqual(os.readlink(link), other, "uninstall removes only its own link")

    def test_refuses_a_settings_file_that_is_not_an_object(self):
        self.write_settings([1, 2])
        done = self.install(check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("not a JSON object", done.stderr)
        self.assertEqual(self.read_settings(), [1, 2])

    def test_help_and_unknown_options(self):
        done = self.install("-h")
        self.assertIn("--uninstall --purge", done.stdout)
        self.assertFalse(os.path.exists(self.claude))
        self.assertEqual(self.install("--frobnicate", check=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
