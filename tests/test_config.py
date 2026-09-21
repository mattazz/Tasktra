import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tasktra.config import ConfigError, ProjectConfig, config_path, initialize_project, load_project_config


class ConfigTests(unittest.TestCase):
    def test_initialize_and_load_defaults(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            created = initialize_project(root, name="Example")
            self.assertEqual(created, config_path(root))
            config = load_project_config(root)
            self.assertEqual(config.name, "Example")
            self.assertEqual(config.enabled_packs, ("core",))
            self.assertEqual(config.validation_commands, ())
            self.assertTrue(config.include_pack_validation_defaults)
            self.assertEqual(config.concurrency_limit, 3)
            self.assertEqual(config.database_path(root), (root / ".tasktra/runtime/tasktra.sqlite").resolve())

    def test_rejects_invalid_project_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = config_path(root)
            destination.parent.mkdir()
            destination.write_text("[project]\nname = ''\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_project_config(root)

    def test_never_overwrites_existing_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root)
            with self.assertRaises(FileExistsError):
                initialize_project(root)

    def test_loads_canonical_validation_argv_arrays(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = config_path(root)
            destination.parent.mkdir()
            destination.write_text(
                """[project]
name = "Example"
config_version = 1

[validation]
commands = [["python", "-c", "print('space and quote')", "C:\\\\path with spaces\\\\"]]
""",
                encoding="utf-8",
            )
            config = load_project_config(root)
            self.assertEqual(
                config.validation_commands,
                (("python", "-c", "print('space and quote')", "C:\\path with spaces\\"),),
            )

    def test_project_can_replace_pack_validation_defaults(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = config_path(root)
            destination.parent.mkdir()
            destination.write_text(
                """[project]
name = "Example"
config_version = 1

[validation]
include_pack_defaults = false
commands = [["python", "-m", "unittest", "tests.test_config"]]
""",
                encoding="utf-8",
            )
            config = load_project_config(root)
            self.assertFalse(config.include_pack_validation_defaults)

    def test_rejects_legacy_string_validation_command_clearly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = config_path(root)
            destination.parent.mkdir()
            destination.write_text(
                """[project]
name = "Example"
config_version = 1

[validation]
commands = ["python -m unittest"]
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "argv array"):
                load_project_config(root)

    def test_database_path_rejects_absolute_and_parent_project_escapes(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "project"
            root.mkdir()
            outside = parent / "external.sqlite"

            with self.assertRaisesRegex(ConfigError, "inside the project root"):
                ProjectConfig(name="escape", database=str(outside)).database_path(root)
            with self.assertRaisesRegex(ConfigError, "inside the project root"):
                ProjectConfig(name="escape", database="../external.sqlite").database_path(root)

    def test_database_path_rejects_symbolic_link_ancestor(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "project"
            outside = root / "real-runtime-root"
            root.mkdir()
            outside.mkdir()
            linked = root / ".tasktra"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaisesRegex(ConfigError, "symbolic link or reparse point"):
                ProjectConfig(name="escape").database_path(root)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_database_path_rejects_windows_junction_ancestor(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "project"
            root.mkdir()
            outside = root / "real-runtime-root"
            outside.mkdir()
            junction = root / ".tasktra"
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"junction creation unavailable: {completed.stderr or completed.stdout}")

            with self.assertRaisesRegex(ConfigError, "symbolic link or reparse point"):
                ProjectConfig(name="escape").database_path(root)
