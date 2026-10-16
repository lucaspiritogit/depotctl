import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from depotctl import config, pipeline, steam
from depotctl.config import TARGETS, ConfigError
from depotctl.proc import CommandError


def write_config(root: Path, shared: str, local: str = "") -> None:
    (root / "depotctl.toml").write_text(textwrap.dedent(shared), encoding="utf-8")
    if local:
        (root / "depotctl.local.toml").write_text(textwrap.dedent(local), encoding="utf-8")


SHARED = """
    [defaults]
    set_live = "testing"

    [projects.my-game]
    name = "My Game"
    path = "my-game"
    steam_app_id = 1234560

    [projects.my-game.depots]
    windows = 1234561
    macos = 1234562
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.root = self.ws  # depotctl.toml lives in the workspace

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_relative_path(self):
        write_config(self.root, SHARED)
        cfg = config.load(self.root)
        p = cfg.project("my-game")
        self.assertEqual(p.path, (self.ws / "my-game").resolve())
        self.assertEqual(p.product_name, "My Game")
        self.assertEqual(p.set_live, "testing")
        self.assertEqual(p.targets, ["windows", "macos"])
        self.assertIn("*steam_appid.txt", p.exclude)
        self.assertEqual(cfg.local.builder, "unity-cli")

    def test_local_overrides(self):
        write_config(self.root, SHARED, """
            steam_user = "builder"
            uploader = "docker"
            [projects.my-game]
            path = "{elsewhere}"
        """.replace("{elsewhere}", (self.ws / "elsewhere").as_posix()))
        cfg = config.load(self.root)
        self.assertEqual(cfg.local.steam_user, "builder")
        self.assertEqual(cfg.project("my-game").path, (self.ws / "elsewhere").resolve())

    def test_repo_and_prefix(self):
        write_config(self.root, SHARED.replace('path = "my-game"',
                     'path = "my-game"\nrepo = "https://example.com/a.git"'), """
            unity_prefix = ["xvfb-run", "-a"]
            [projects.my-game]
            repo = "git@example.com:a.git"
        """)
        cfg = config.load(self.root)
        self.assertEqual(cfg.project("my-game").repo, "git@example.com:a.git")  # local wins
        self.assertEqual(cfg.local.unity_prefix, ["xvfb-run", "-a"])

    def test_lookup_by_name(self):
        write_config(self.root, SHARED)
        cfg = config.load(self.root)
        self.assertEqual(cfg.project("my game").key, "my-game")
        with self.assertRaises(ConfigError):
            cfg.project("nope")

    def test_bad_local_setting(self):
        write_config(self.root, SHARED, 'uplaoder = "docker"\n')
        with self.assertRaises(ConfigError):
            config.load(self.root)

    def test_env_steam_user(self):
        write_config(self.root, SHARED)
        with mock.patch.dict(os.environ, {"DEPOTCTL_STEAM_USER": "envuser"}):
            self.assertEqual(config.load(self.root).local.steam_user, "envuser")

    def test_upload_validation(self):
        write_config(self.root, SHARED.replace("macos = 1234562", "macos = 0"))
        cfg = config.load(self.root)
        p = cfg.project("my-game")
        problems = config.validate_for_upload(cfg, p, p.target_list())
        self.assertTrue(any("macos depot" in x for x in problems))
        self.assertTrue(any("steam_user" in x for x in problems))


class InitAndDiscoveryTests(unittest.TestCase):
    def test_init_discovers_projects(self):
        from depotctl import cli
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            game = ws / "Cool Game"
            (game / "ProjectSettings").mkdir(parents=True)
            (game / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.4f1\n")
            (game / "ProjectSettings/ProjectSettings.asset").write_text("  productName: Cool Game\n")
            (game / "steam_appid.txt").write_text("1234560\n")
            (ws / "not-unity").mkdir()

            self.assertEqual(cli.main(["init", str(ws)]), 0)
            self.assertTrue((ws / "depotctl.local.toml").is_file())
            cfg = config.load(ws)
            self.assertEqual(list(cfg.projects), ["cool-game"])
            p = cfg.project("cool-game")
            self.assertEqual(p.path, game.resolve())
            self.assertEqual(p.steam_app_id, 1234560)
            self.assertEqual(p.product_name, "Cool Game")
            self.assertEqual(cli.main(["init", str(ws)]), 1)  # refuses to overwrite

    def test_init_without_projects_writes_example(self):
        from depotctl import cli
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cli.main(["init", d]), 0)
            self.assertIn("my-game", config.load(Path(d)).projects)

    def test_find_root_walks_up(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d).resolve()
            write_config(ws, SHARED)
            sub = ws / "my-game" / "Assets"
            sub.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"DEPOTCTL_CONFIG": ""}), \
                 mock.patch.object(Path, "cwd", return_value=sub):
                self.assertEqual(config.find_root(), ws)

    def test_bundled_resources(self):
        from depotctl import resources
        self.assertTrue((resources.path("docker") / "Dockerfile").is_file())
        self.assertTrue((resources.path("docker") / "depotctl-steam.sh").is_file())
        self.assertTrue((resources.path("unity") / "Editor" / "DepotctlBuild.cs").is_file())
        self.assertIn("# ---- projects", resources.path("templates/depotctl.toml").read_text())


class VdfTests(unittest.TestCase):
    def test_app_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_config(root, SHARED)
            p = config.load(root).project("my-game")
        text = steam.app_build_vdf(p, p.target_list(), desc='my-game abc "x"',
                                   content_root="/content/", build_output="/job/output/",
                                   set_live="testing", preview=False)
        self.assertIn('"AppID"\t\t"1234560"', text)
        self.assertIn('"SetLive"\t\t"testing"', text)
        self.assertIn('"LocalPath"\t\t"windows/*"', text)
        self.assertIn('"LocalPath"\t\t"macos/*"', text)
        self.assertIn('"1234562"', text)
        self.assertIn('"FileExclusion"\t\t"*.pdb"', text)
        self.assertIn("my-game abc 'x'", text)  # quotes can't break the VDF
        self.assertEqual(text.count("{"), text.count("}"))
        no_live = steam.app_build_vdf(p, p.target_list(), desc="d", content_root="/c/",
                                      build_output="/o/", set_live=None, preview=True)
        self.assertNotIn("SetLive", no_live)
        self.assertIn('"Preview"\t\t"1"', no_live)


class GuardTests(unittest.TestCase):
    def test_default_branch_guard(self):
        p = mock.Mock(set_live="testing")
        self.assertEqual(pipeline._resolve_set_live(p, pipeline.Options()), "testing")
        self.assertIsNone(pipeline._resolve_set_live(p, pipeline.Options(set_live="")))
        with self.assertRaises(ConfigError):
            pipeline._resolve_set_live(p, pipeline.Options(set_live="default"))
        self.assertEqual(
            pipeline._resolve_set_live(p, pipeline.Options(set_live="default", allow_default_branch=True)),
            "default")

    def test_native_windows_refuses_macos(self):
        cfg = mock.Mock()
        cfg.local.uploader = "native"
        with mock.patch.object(steam, "HOST", "Windows"):
            with self.assertRaises(CommandError):
                steam.pick_uploader(cfg, [TARGETS["windows"], TARGETS["macos"]])
            self.assertEqual(steam.pick_uploader(cfg, [TARGETS["windows"]]), "native")
        cfg.local.uploader = "auto"
        with mock.patch.object(steam, "HOST", "Windows"):
            self.assertEqual(steam.pick_uploader(cfg, [TARGETS["macos"]]), "docker")
        with mock.patch.object(steam, "HOST", "Darwin"):
            self.assertEqual(steam.pick_uploader(cfg, [TARGETS["macos"]]), "native")

    def test_modules_needed(self):
        from depotctl import unity
        with mock.patch.object(unity, "HOST", "Windows"):
            self.assertEqual(unity.modules_needed([TARGETS["windows"], TARGETS["macos"]]), ["mac-mono"])
        with mock.patch.object(unity, "HOST", "Darwin"):
            self.assertEqual(unity.modules_needed([TARGETS["windows"], TARGETS["macos"]]), ["windows-mono"])


if __name__ == "__main__":
    unittest.main()
