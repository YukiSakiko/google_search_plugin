"""Offline regression tests for the WebUI-only configuration layout."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

import sys
import unittest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from maibot_sdk.config import generate_plugin_config_schema  # noqa: E402

from plugins.google_search_plugin.config import GoogleSearchPluginConfig  # noqa: E402
from plugins.google_search_plugin.config_ui import build_config_ui_schema  # noqa: E402


class ConfigUiSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = generate_plugin_config_schema(
            GoogleSearchPluginConfig,
            plugin_id="config-ui-test",
            plugin_name="Configuration Test",
            plugin_version="test-version",
        )
        self.source_snapshot = deepcopy(self.source)
        self.model_schema_snapshot = GoogleSearchPluginConfig.model_json_schema()
        self.ui = build_config_ui_schema(self.source)

    def iter_fields(self) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
        for section_id, section in self.ui["sections"].items():
            for field_name, field in section["fields"].items():
                yield section_id, section["name"], field_name, field

    def test_each_original_field_appears_once_under_its_real_section(self) -> None:
        expected = Counter(
            (section_name, field_name)
            for section_name, section in self.source["sections"].items()
            for field_name in section["fields"]
        )
        actual = Counter((section_name, field_name) for _, section_name, field_name, _ in self.iter_fields())
        self.assertEqual(actual, expected)
        for section_id, section_name, field_name, field in self.iter_fields():
            with self.subTest(section=section_id, field=field_name):
                self.assertIn(section_name, self.source["sections"])
                self.assertEqual(field["name"], field_name)
                self.assertIn(field_name, self.source["sections"][section_name]["fields"])

    def test_tabs_cover_every_block_and_keep_advanced_blocks_collapsed(self) -> None:
        self.assertEqual(self.ui["layout"]["type"], "tabs")
        tabs = self.ui["layout"]["tabs"]
        self.assertEqual([tab["id"] for tab in tabs], ["basic", "engines", "advanced"])
        self.assertEqual(
            Counter(section_id for tab in tabs for section_id in tab["sections"]),
            Counter(self.ui["sections"].keys()),
        )
        for tab in tabs:
            self.assertTrue(tab["title"].strip())
            for section_id in tab["sections"]:
                with self.subTest(tab=tab["id"], section=section_id):
                    self.assertEqual(
                        self.ui["sections"][section_id]["collapsed"],
                        tab["id"] == "advanced",
                    )
        basic_field_count = sum(len(self.ui["sections"][section_id]["fields"]) for section_id in tabs[0]["sections"])
        self.assertGreater(basic_field_count, 0)
        self.assertLessEqual(basic_field_count, 10)

    def test_fields_have_localized_labels_and_help(self) -> None:
        for section_id, _, field_name, field in self.iter_fields():
            with self.subTest(section=section_id, field=field_name):
                self.assertRegex(field["label"], r"[\u4e00-\u9fff]")
                self.assertIsInstance(field["hint"], str)
                self.assertTrue(field["hint"].strip())
                self.assertNotIn("环境变量", field["hint"])
                self.assertNotIn("环境变量", field["placeholder"] or "")
        for section in self.ui["sections"].values():
            self.assertNotIn("环境变量", section["description"])

    def test_select_choices_never_use_radix_reserved_empty_values(self) -> None:
        for section_id, _, field_name, field in self.iter_fields():
            if field["ui_type"] == "select":
                with self.subTest(section=section_id, field=field_name):
                    self.assertNotIn("", [str(choice) for choice in field["choices"]])

    def test_anysearch_zone_has_an_explicit_automatic_choice(self) -> None:
        field = self.ui["sections"]["anysearch_advanced"]["fields"]["anysearch_zone"]
        self.assertEqual(field["ui_type"], "select")
        self.assertEqual(field["default"], "auto")
        self.assertEqual(field["choices"], ["auto", "cn", "intl"])

    def test_api_key_lists_are_in_their_provider_blocks_with_plaintext_warnings(self) -> None:
        self.assertNotIn("api_keys", self.ui["sections"])
        engines_tab = self.ui["layout"]["tabs"][1]
        for provider in ("tavily", "anysearch", "exa", "you", "deepseek"):
            with self.subTest(provider=provider):
                section = self.ui["sections"][provider]
                self.assertIn(provider, engines_tab["sections"])
                self.assertEqual(section["name"], "engines")
                self.assertFalse(section["collapsed"])
                self.assertNotIn(f"{provider}_api_key", self.source["sections"]["engines"]["fields"])
                self.assertEqual(set(section["fields"]), {f"{provider}_enabled", f"{provider}_api_keys"})
                field = section["fields"][f"{provider}_api_keys"]
                self.assertEqual(field["ui_type"], "list")
                self.assertEqual(field["type"], "array")
                self.assertEqual(field["item_type"], "string")
                self.assertIn("每项填写一把", field["hint"])
                self.assertIn("添加一项即可", field["hint"])
                self.assertIn("明文", field["hint"])
                self.assertEqual(field["placeholder"], "添加一把 API 密钥")

    def test_cookie_uses_password_input_and_only_plugin_metadata_is_read_only(self) -> None:
        expected_secrets = {("search_backend", "zhihu_cookies")}
        password_fields = {
            (section_name, field_name)
            for _, section_name, field_name, field in self.iter_fields()
            if field["ui_type"] == "password"
        }
        self.assertEqual(password_fields, expected_secrets)
        disabled_fields = {
            (section_name, field_name) for _, section_name, field_name, field in self.iter_fields() if field["disabled"]
        }
        self.assertEqual(
            disabled_fields,
            {("plugin", "name"), ("plugin", "version"), ("plugin", "config_version")},
        )

    def test_source_schema_is_unchanged_and_layout_is_deterministic(self) -> None:
        self.assertEqual(self.source, self.source_snapshot)
        self.assertEqual(GoogleSearchPluginConfig.model_json_schema(), self.model_schema_snapshot)
        self.assertEqual(build_config_ui_schema(self.source), self.ui)
        for key in self.source.keys() - {"sections", "layout"}:
            self.assertEqual(self.ui[key], self.source[key])

    def test_defaults_types_choices_and_validation_metadata_are_preserved(self) -> None:
        presentation_keys = {"label", "hint", "order", "ui_type", "placeholder", "disabled"}
        for section_id, section_name, field_name, field in self.iter_fields():
            original = self.source["sections"][section_name]["fields"][field_name]
            with self.subTest(section=section_id, field=field_name):
                self.assertEqual(field.keys(), original.keys())
                self.assertEqual(
                    {key: value for key, value in field.items() if key not in presentation_keys},
                    {key: value for key, value in original.items() if key not in presentation_keys},
                )

    def test_display_block_edits_write_back_to_existing_config_sections(self) -> None:
        saved = {}
        for _, section_name, field_name, field in self.iter_fields():
            saved.setdefault(section_name, {})[field_name] = deepcopy(field["default"])
        defaults = GoogleSearchPluginConfig()
        self.assertEqual(GoogleSearchPluginConfig.model_validate(saved).model_dump(), defaults.model_dump())

        edits = {
            ("search", "default_engine"): "google",
            ("search", "proxy"): "http://127.0.0.1:7890",
            ("google", "google_enabled"): True,
            ("tavily", "tavily_api_keys"): ["fake-tavily-key"],
            ("anysearch", "anysearch_api_keys"): ["fake-anysearch-key"],
            ("exa", "exa_api_keys"): ["fake-exa-key"],
            ("anysearch_advanced", "anysearch_zone"): "cn",
            ("anysearch_advanced", "anysearch_language"): "zh",
            ("anysearch_advanced", "anysearch_params"): '{"page": 2}',
            ("exa_advanced", "exa_search_type"): "deep-lite",
            ("exa_advanced", "exa_include_domains"): ["example.com"],
            ("exa_content", "exa_include_text"): True,
            ("exa_content", "exa_text_max_characters"): 1000,
            ("exa_content", "exa_max_age_hours"): 24,
            ("you", "you_api_keys"): ["fake-you-key-1", "fake-you-key-2"],
            ("deepseek", "deepseek_api_keys"): ["fake-deepseek-key"],
            ("fetch_advanced", "content_timeout"): 12,
            ("model", "model_name"): "utils",
            ("model_advanced", "temperature"): 0.2,
            ("translation", "enabled"): False,
            ("translation_advanced", "max_retries"): 2,
        }
        expected = defaults.model_dump()
        for (section_id, field_name), value in edits.items():
            section = self.ui["sections"][section_id]
            source_name = section["name"]
            saved[source_name][section["fields"][field_name]["name"]] = value
            expected[source_name][field_name] = value

        validated = GoogleSearchPluginConfig.model_validate(saved).model_dump()
        self.assertEqual(validated, expected)
        self.assertEqual(set(saved), set(self.source["sections"]))
        self.assertEqual(set(validated), set(self.source["sections"]))
        self.assertNotIn("fetch_advanced", validated)
        self.assertNotIn("api_keys", validated)

    def test_new_engines_are_selectable_and_exa_type_is_in_advanced_settings(self) -> None:
        choices = self.ui["sections"]["search"]["fields"]["default_engine"]["choices"]
        for provider in ("anysearch", "exa"):
            with self.subTest(provider=provider):
                self.assertIn(provider, choices)
                config = GoogleSearchPluginConfig.model_validate({"search_backend": {"default_engine": provider}})
                self.assertEqual(config.search_backend.default_engine, provider)
                self.assertFalse(getattr(config.engines, f"{provider}_enabled"))
        section = self.ui["sections"]["exa_advanced"]
        self.assertTrue(section["collapsed"])
        self.assertEqual(section["name"], "engines")
        field = section["fields"]["exa_search_type"]
        self.assertEqual(field["default"], "auto")
        self.assertEqual(set(field["choices"]), {"auto", "lite", "deep-lite", "deep"})
        self.assertIn("留空", self.ui["sections"]["anysearch"]["fields"]["anysearch_api_keys"]["hint"])
        self.assertIn("匿名", self.ui["sections"]["anysearch"]["fields"]["anysearch_api_keys"]["hint"])

    def test_new_provider_options_are_grouped_in_collapsed_advanced_blocks(self) -> None:
        expected_fields = {
            "anysearch_advanced": {
                "anysearch_tag",
                "anysearch_zone",
                "anysearch_language",
                "anysearch_params",
                "anysearch_format",
            },
            "exa_advanced": {
                "exa_search_type",
                "exa_category",
                "exa_include_domains",
                "exa_exclude_domains",
                "exa_start_published_date",
                "exa_end_published_date",
                "exa_user_location",
                "exa_moderation",
                "exa_objective",
                "exa_additional_queries",
            },
            "exa_content": {
                "exa_include_highlights",
                "exa_highlights_query",
                "exa_highlights_max_characters",
                "exa_include_text",
                "exa_text_max_characters",
                "exa_max_age_hours",
                "exa_livecrawl_timeout_ms",
            },
        }
        advanced_sections = self.ui["layout"]["tabs"][2]["sections"]
        for section_id, field_names in expected_fields.items():
            with self.subTest(section=section_id):
                section = self.ui["sections"][section_id]
                self.assertIn(section_id, advanced_sections)
                self.assertTrue(section["collapsed"])
                self.assertEqual(section["name"], "engines")
                self.assertEqual(set(section["fields"]), field_names)
                self.assertTrue(all(not name.endswith("_api_keys") for name in section["fields"]))
        self.assertEqual(advanced_sections[advanced_sections.index("tavily_advanced") + 1], "anysearch_advanced")

    def test_advanced_help_explains_provider_constraints_and_direct_content_limits(self) -> None:
        fields = {
            field_name: field
            for section_id in ("anysearch_advanced", "exa_advanced", "exa_content")
            for field_name, field in self.ui["sections"][section_id]["fields"].items()
        }
        self.assertIn("JSON", fields["anysearch_params"]["hint"])
        self.assertIn("lite", fields["exa_additional_queries"]["hint"])
        self.assertIn("company/people", fields["exa_start_published_date"]["hint"])
        self.assertIn("people", fields["exa_exclude_domains"]["hint"])
        for field_name in ("exa_highlights_max_characters", "exa_text_max_characters"):
            with self.subTest(field=field_name):
                self.assertIn("0", fields[field_name]["hint"])
                self.assertIn("默认", fields[field_name]["hint"])
        self.assertIn("-2", fields["exa_max_age_hours"]["hint"])
        self.assertIn("720", fields["exa_max_age_hours"]["hint"])
        self.assertIn("90000", fields["exa_livecrawl_timeout_ms"]["hint"])
        length_hint = self.ui["sections"]["fetch_advanced"]["fields"]["max_content_length"]["hint"]
        self.assertIn("AnySearch/Exa", length_hint)
        self.assertIn("DeepSeek", length_hint)

    def test_engine_card_descriptions_state_use_and_network_without_repeating_setup(self) -> None:
        for section_id in ("google", "bing", "sogou", "duckduckgo", "tavily", "anysearch", "exa", "you", "deepseek"):
            with self.subTest(section=section_id):
                description = self.ui["sections"][section_id]["description"]
                self.assertIn("网络", description)
                self.assertNotIn("开启后", description)
                self.assertNotIn("密钥列表", description)
                self.assertNotIn("国内可直连", description)


if __name__ == "__main__":
    unittest.main()
