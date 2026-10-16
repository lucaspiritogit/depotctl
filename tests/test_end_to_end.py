"""Full deploy against a real git remote and fake unity / docker / steamcmd (POSIX only)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

FAKE_UNITY = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as f:
    f.write("unity " + " ".join(args) + "\n")
if args[:1] == ["--version"]:
    print("1.0.0-beta.13"); sys.exit(0)
if args[:1] == ["editors"]:
    print(json.dumps({"success": True, "data": [{"version": "6000.6.4f1",
          "path": "/fake/6000.6.4f1", "modules": ["mac-mono", "windows-mono"]}]})); sys.exit(0)
if args[:1] == ["build"]:
    out = Path(args[args.index("--output-path") + 1])
    target = args[args.index("--target") + 1]
    print(f"[editor] building {target}", file=sys.stderr)
    if os.environ.get("FAKE_BUILD_FAIL") == target:
        print("[editor] error CS1002: ; expected", file=sys.stderr)
        print(json.dumps({"success": False, "errors": [{"code": "BUILD_FAILED", "message": "compile errors"}]}))
        sys.exit(6)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".app":
        (out / "Contents/MacOS").mkdir(parents=True)
        (out / "Contents/MacOS" / out.stem).write_text("bin")
    else:
        out.write_text("exe")
    (out.parent / "steam_appid.txt").write_text("1234560")
    print(json.dumps({"success": True, "data": {"outputPath": str(out)}}))
    sys.exit(0)
sys.exit(0)
"""

# docker: info/image inspect succeed; run translates the mounts and executes the real
# entrypoint script with a fake steamcmd and a stub rcodesign.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("docker " + " ".join(args) + "\n")
if args[0] != "run":
    sys.exit(0)
env = dict(os.environ)
i = 0
while i < len(args):
    a = args[i]
    if a == "--mount":
        parts = dict(p.split("=", 1) for p in args[i + 1].split(",") if "=" in p)
        if parts.get("target") == "/input": env["INPUT_DIR"] = parts["source"]
        if parts.get("target") == "/job": env["JOB_DIR"] = parts["source"]
        i += 2; continue
    if a == "-e":
        k, v = args[i + 1].split("=", 1); env[k] = v; i += 2; continue
    i += 1
env["CONTENT_DIR"] = env["FAKE_CONTENT"]
env["STEAMCMD"] = env["FAKE_STEAMCMD"]
sys.exit(subprocess.call(["bash", env["ENTRYPOINT"], args[-1]], env=env))
"""

FAKE_STEAMCMD = """#!/usr/bin/env bash
echo "steamcmd $*" >> "$FAKE_LOG"
vdf=""; prev=""
for a in "$@"; do [ "$prev" = "+run_app_build" ] && vdf="$a"; prev="$a"; done
cp "$vdf" "$FAKE_VDF_COPY"
echo "Logging in user '$STEAM_USER' to Steam Public...OK"
echo "Successfully finished AppID 1234560 build (BuildID 987654)."
"""

FAKE_RCODESIGN = """#!/usr/bin/env bash
echo "rcodesign $*" >> "$FAKE_LOG"
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipIf(os.name == "nt", "uses POSIX fake binaries")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        ws = self.tmp / "studio"
        self.root = ws  # depotctl.toml lives next to the game projects
        self.root.mkdir(parents=True)
        (self.root / "depotctl.toml").write_text(textwrap.dedent("""
            [defaults]
            set_live = "testing"
            [projects.my-game]
            name = "My Game"
            path = "my-game"
            steam_app_id = 1234560
            [projects.my-game.depots]
            windows = 1234561
            macos = 1234562
        """))
        (self.root / "depotctl.local.toml").write_text('steam_user = "builder"\nuploader = "docker"\n')

        # game repo with an upstream that receives a new commit after cloning
        upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(upstream)], check=True, capture_output=True)
        seed = self.tmp / "seed"
        git(self.tmp, "clone", str(upstream), str(seed))
        for repo in (seed,):
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "t")
        (seed / "ProjectSettings").mkdir()
        (seed / "ProjectSettings/ProjectVersion.txt").write_text(
            "m_EditorVersion: 6000.6.4f1\nm_EditorVersionWithRevision: 6000.6.4f1 (12bfff696524)\n")
        git(seed, "add", "."); git(seed, "commit", "-m", "init"); git(seed, "push", "origin", "main")
        self.game = ws / "my-game"
        git(self.tmp, "clone", str(upstream), str(self.game))
        (seed / "README").write_text("new")
        git(seed, "add", "."); git(seed, "commit", "-m", "Add forge level"); git(seed, "push", "origin", "main")

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in [("unity", FAKE_UNITY), ("docker", FAKE_DOCKER),
                           ("steamcmd", FAKE_STEAMCMD), ("rcodesign", FAKE_RCODESIGN)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.vdf_copy = self.tmp / "uploaded.vdf"
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DEPOTCTL_CONFIG": str(self.root / "depotctl.toml"),
            "FAKE_LOG": str(self.log),
            "FAKE_CONTENT": str(self.tmp / "content"),
            "FAKE_STEAMCMD": str(bin_dir / "steamcmd"),
            "FAKE_VDF_COPY": str(self.vdf_copy),
            "ENTRYPOINT": str(REPO / "docker" / "depotctl-steam.sh"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def depotctl(self, *args, **extra_env):
        return subprocess.run([sys.executable, "-m", "depotctl", *args],
                              env={**os.environ, **self.env, **extra_env},
                              capture_output=True, text=True, cwd=self.tmp)

    def test_deploy(self):
        r = self.depotctl("deploy", "my-game")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Steam BuildID 987654", r.stdout)
        self.assertIn("Add forge level", r.stdout)  # pulled the new upstream commit

        builds = sorted((self.root / "depotctl-builds" / "my-game").iterdir())
        self.assertEqual(len(builds), 1)
        m = json.loads((builds[0] / "manifest.json").read_text())
        self.assertEqual(m["status"], "built")
        self.assertEqual(m["targets"], {"windows": "windows/My Game.exe",
                                        "macos": "macos/My Game.app"})
        self.assertEqual(m["uploads"][0]["build_id"], "987654")

        vdf = self.vdf_copy.read_text()
        self.assertIn('"ContentRoot"\t\t"/content/"', vdf)
        self.assertIn('"SetLive"\t\t"testing"', vdf)
        calls = self.log.read_text()
        self.assertIn("--target StandaloneWindows64", calls)
        self.assertIn("--target StandaloneOSX", calls)
        self.assertNotIn("install-modules", calls)  # modules reported installed
        self.assertIn("rcodesign sign", calls)
        self.assertIn("+login builder +run_app_build", calls)
        app_bin = self.tmp / "content/macos/My Game.app/Contents/MacOS/My Game"
        self.assertTrue(os.access(app_bin, os.X_OK))

        # re-upload the same build to another branch without rebuilding
        r = self.depotctl("upload", "my-game", "--set-live", "qa", "-t", "windows")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('"SetLive"\t\t"qa"', self.vdf_copy.read_text())
        self.assertNotIn('"1234562"', self.vdf_copy.read_text())

        r = self.depotctl("builds", "my-game")
        self.assertIn("BuildID 987654", r.stdout)

    def test_clones_missing_project(self):
        upstream = self.tmp / "upstream.git"
        shutil.rmtree(self.game)
        cfg = (self.root / "depotctl.toml").read_text().replace(
            'path = "my-game"', f'path = "my-game"\nrepo = "{upstream}"')
        (self.root / "depotctl.toml").write_text(cfg)
        r = self.depotctl("deploy", "my-game", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run] git clone", r.stdout)
        self.assertFalse(self.game.exists())

        (self.root / "depotctl.local.toml").write_text(
            'steam_user = "builder"\nuploader = "docker"\nunity_prefix = ["env", "WRAPPED=1"]\n')
        r = self.depotctl("deploy", "my-game")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cloned", r.stdout)
        self.assertIn("$ env WRAPPED=1 unity build", r.stdout)
        self.assertIn("Steam BuildID 987654", r.stdout)
        self.assertTrue((self.game / "ProjectSettings/ProjectVersion.txt").is_file())

        r = self.depotctl("clone", "--all")
        self.assertIn("already at", r.stdout)

    def test_clone_without_repo_fails_early(self):
        shutil.rmtree(self.game)
        r = self.depotctl("deploy", "my-game")
        self.assertEqual(r.returncode, 1)
        self.assertIn("or `repo` so depotctl can clone it", r.stderr)

    def test_build_failure_shows_log_tail(self):
        r = self.depotctl("deploy", "my-game", FAKE_BUILD_FAIL="StandaloneOSX")
        self.assertEqual(r.returncode, 1)
        self.assertIn("compile errors", r.stderr)
        self.assertIn("error CS1002", r.stderr)
        self.assertNotIn("steamcmd", self.log.read_text())

    def test_dirty_tree_refused(self):
        (self.game / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.4f1\n")
        r = self.depotctl("deploy", "my-game")
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted changes", r.stderr)

    def test_dry_run_changes_nothing(self):
        r = self.depotctl("deploy", "my-game", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        self.assertFalse((self.root / "depotctl-builds" / "my-game").exists())
        self.assertNotIn("Add forge level", r.stdout)  # nothing pulled

    def test_preflight_catches_missing_depot(self):
        cfg = (self.root / "depotctl.toml").read_text().replace("macos = 1234562", "macos = 0")
        (self.root / "depotctl.toml").write_text(cfg)
        r = self.depotctl("deploy", "my-game")
        self.assertEqual(r.returncode, 1)
        self.assertIn("macos depot id is not set", r.stderr)
        self.assertFalse(self.log.exists() and "unity build" in self.log.read_text())


if __name__ == "__main__":
    unittest.main()
