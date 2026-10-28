"""Exercise actual Runner configuration persistence using disposable TOML files."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import json
import sys
import tomllib
import unittest

import tomlkit

_HOST_ROOT = Path(__file__).resolve().parents[3]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from plugins.google_search_plugin.plugin import GoogleSearchPlugin  # noqa: E402

# Import the real Runner without initializing the host's global file logging.
_HOST_LOGGER = ModuleType("src.common.logger")
_HOST_LOGGER.get_logger = Mock(return_value=Mock(name="config_test_host_logger"))
_HOST_LOGGER.get_console_handler = Mock()
_HOST_LOGGER.initialize_logging = Mock()
with patch.dict(sys.modules, {"src.common.logger": _HOST_LOGGER}):
    from src.plugin_runtime.runner.runner_main import PluginRunner

_NEW_ENGINE_DEFAULTS = {
    "anysearch_enabled": False,
    "anysearch_api_keys": [],
    "exa_enabled": False,
    "exa_api_keys": [],
    "exa_search_type": "auto",
}


class RunnerConfigUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="search-config-upgrade-")
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.toml"
        self.plugin = GoogleSearchPlugin()
        # Configuration application does not need a live RPC client or Runner loop.
        self.runner = PluginRunner.__new__(PluginRunner)
        self.meta = SimpleNamespace(
            plugin_id="google_search_config_test",
            plugin_dir=self.directory.name,
            instance=self.plugin,
        )

    def previous_engine_config(self) -> dict:
        config = self.plugin.build_default_config()
        for name in tuple(config["engines"]):
            if name.startswith(("anysearch_", "exa_")):
                config["engines"].pop(name)
        config["search_backend"].update(default_engine="tavily", timeout=37)
        config["engines"].update(
            tavily_enabled=True,
            tavily_api_keys=["fixture-only-key"],
            you_api_keys=["fixture-only-you-key"],
            deepseek_api_keys=["fixture-only-deepseek-key"],
        )
        return config

    def write_fixture(self, config: dict) -> bytes:
        self.config_path.write_text(
            "# Preserve this user comment.\n" + tomlkit.dumps(config),
            encoding="utf-8",
            newline="\n",
        )
        return self.config_path.read_bytes()

    def read_persisted_config(self) -> dict:
        with self.config_path.open("rb") as handle:
            return tomllib.load(handle)

    def assert_new_engine_defaults(self, config: dict) -> None:
        defaults = {
            name: value
            for name, value in self.plugin.build_default_config()["engines"].items()
            if name.startswith(("anysearch_", "exa_"))
        }
        self.assertGreaterEqual(defaults.keys(), _NEW_ENGINE_DEFAULTS.keys())
        for name, expected in defaults.items():
            with self.subTest(field=name):
                self.assertEqual(config["engines"][name], expected)

    def assert_user_values_preserved(self, config: dict) -> None:
        self.assertEqual(config["search_backend"]["default_engine"], "tavily")
        self.assertEqual(config["search_backend"]["timeout"], 37)
        self.assertTrue(config["engines"]["tavily_enabled"])
        self.assertEqual(config["engines"]["tavily_api_keys"], ["fixture-only-key"])
        self.assertEqual(config["engines"]["you_api_keys"], ["fixture-only-you-key"])
        self.assertEqual(config["engines"]["deepseek_api_keys"], ["fixture-only-deepseek-key"])

    def test_new_install_creates_and_persists_the_current_defaults(self) -> None:
        self.assertFalse(self.config_path.exists())
        normalized = self.runner._apply_plugin_config(self.meta)
        self.assertTrue(self.config_path.exists())
        persisted = self.read_persisted_config()
        self.assertEqual(persisted, normalized)
        self.assertEqual(persisted, self.plugin.build_default_config())
        self.assert_new_engine_defaults(persisted)
        self.assertEqual(self.plugin.get_plugin_config_data(), persisted)

    def test_legacy_empty_anysearch_zone_is_upgraded_and_persisted_as_auto(self) -> None:
        raw = self.plugin.build_default_config()
        raw["plugin"]["config_version"] = "4.2.0"
        raw["engines"].update(anysearch_zone="", anysearch_enabled=True)
        self.write_fixture(raw)
        normalized = self.runner._apply_plugin_config(self.meta)
        persisted = self.read_persisted_config()
        self.assertEqual(persisted, normalized)
        self.assertEqual(persisted["engines"]["anysearch_zone"], "auto")
        self.assertTrue(persisted["engines"]["anysearch_enabled"])
        self.assertEqual(
            persisted["plugin"]["config_version"], self.plugin.build_default_config()["plugin"]["config_version"]
        )

    def test_same_version_missing_fields_are_filled_in_memory_but_not_persisted(self) -> None:
        raw = self.previous_engine_config()
        original_bytes = self.write_fixture(raw)
        normalization = self.runner._normalize_plugin_config(
            self.plugin,
            raw,
            suppress_errors=False,
            enforce_version=True,
        )
        self.assertTrue(normalization.changed)
        self.assertFalse(normalization.should_persist)
        with patch.object(PluginRunner, "_save_plugin_config", wraps=PluginRunner._save_plugin_config) as save:
            normalized = self.runner._apply_plugin_config(self.meta)
        save.assert_not_called()
        self.assert_new_engine_defaults(normalized)
        self.assert_user_values_preserved(normalized)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)
        self.assertEqual(self.read_persisted_config(), raw)
        self.assertEqual(self.plugin.get_plugin_config_data(), normalized)

    def test_older_version_rebuilds_and_persists_defaults_preserving_user_values_and_comments(self) -> None:
        for previous_version in ("4.1.0", "4.0.0"):
            with self.subTest(previous_version=previous_version):
                raw = self.previous_engine_config()
                raw["plugin"]["config_version"] = previous_version
                snapshot = deepcopy(raw)
                self.write_fixture(raw)
                normalization = self.runner._normalize_plugin_config(
                    self.plugin,
                    raw,
                    suppress_errors=False,
                    enforce_version=True,
                )
                self.assertTrue(normalization.changed)
                self.assertTrue(normalization.should_persist)
                normalized = self.runner._apply_plugin_config(self.meta)
                persisted = self.read_persisted_config()
                self.assertEqual(raw, snapshot)
                self.assertEqual(persisted, normalized)
                self.assert_new_engine_defaults(persisted)
                self.assert_user_values_preserved(persisted)
                self.assertEqual(
                    persisted["plugin"]["config_version"],
                    self.plugin.build_default_config()["plugin"]["config_version"],
                )
                self.assertIn("# Preserve this user comment.", self.config_path.read_text(encoding="utf-8"))

    def test_version_upgrade_preserves_and_migrates_legacy_single_keys_before_rewriting(self) -> None:
        raw = self.previous_engine_config()
        raw["plugin"]["config_version"] = "4.1.0"
        for provider in ("tavily", "you", "deepseek"):
            raw["engines"][f"{provider}_api_key"] = raw["engines"].pop(f"{provider}_api_keys")[0]
        self.write_fixture(raw)
        normalized = self.runner._apply_plugin_config(self.meta)
        persisted = self.read_persisted_config()
        self.assertEqual(persisted, normalized)
        for provider in ("tavily", "you", "deepseek"):
            self.assertNotIn(f"{provider}_api_key", persisted["engines"])
        self.assert_user_values_preserved(persisted)
        self.assert_new_engine_defaults(persisted)

    def test_complete_current_config_is_not_rewritten_on_load(self) -> None:
        raw = self.plugin.build_default_config()
        original_bytes = self.write_fixture(raw)
        with patch.object(PluginRunner, "_save_plugin_config", wraps=PluginRunner._save_plugin_config) as save:
            normalized = self.runner._apply_plugin_config(self.meta)
        save.assert_not_called()
        self.assertEqual(normalized, raw)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_version_upgrade_keeps_anysearch_parameter_json_as_an_editable_string(self) -> None:
        raw = self.previous_engine_config()
        raw["plugin"]["config_version"] = "4.1.0"
        parameter_json = '{ "library": "golang", "version": "1.22", "paths": ["fmt", "net/http"] }'
        raw["engines"]["anysearch_params"] = "  " + parameter_json + "  "
        self.write_fixture(raw)
        normalized = self.runner._apply_plugin_config(self.meta)
        persisted = self.read_persisted_config()
        self.assertEqual(persisted, normalized)
        self.assert_user_values_preserved(persisted)
        self.assertIsInstance(persisted["engines"]["anysearch_params"], str)
        self.assertEqual(persisted["engines"]["anysearch_params"], parameter_json)
        self.assertEqual(json.loads(persisted["engines"]["anysearch_params"]), json.loads(parameter_json))

    def test_invalid_anysearch_parameters_are_rejected_without_changing_the_file(self) -> None:
        for invalid_json in ("[]", "null", "42", "invalid", '{"library":'):
            with self.subTest(invalid_json=invalid_json):
                raw = self.previous_engine_config()
                raw["plugin"]["config_version"] = "4.1.0"
                raw["engines"]["anysearch_params"] = invalid_json
                original_bytes = self.write_fixture(raw)
                with self.assertRaises(ValueError):
                    self.runner._apply_plugin_config(self.meta)
                self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_versionless_legacy_config_is_rejected_before_migration_and_left_unchanged(self) -> None:
        for metadata in (None, {"version": "4.0.0"}, {"config_version": ""}):
            with self.subTest(metadata=metadata):
                raw = self.previous_engine_config()
                if metadata is None:
                    raw.pop("plugin")
                else:
                    raw["plugin"] = metadata
                original_bytes = self.write_fixture(raw)
                normalization = self.runner._normalize_plugin_config(self.plugin, raw, suppress_errors=True)
                self.assertIn("config_version", normalization.config_error)
                self.assertFalse(normalization.should_persist)
                with self.assertRaisesRegex(ValueError, "config_version"):
                    self.runner._apply_plugin_config(self.meta)
                self.assertEqual(self.config_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
