"""Offline validation and configuration wiring for API search options."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import json
import sys
import unittest

from pydantic import ValidationError

_HOST_ROOT = Path(__file__).resolve().parents[3]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.engine_chain import _build_common_cfg, _build_engine_dict  # noqa: E402


class SearchEngineOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        guard = patch("aiohttp.ClientSession", side_effect=AssertionError("Configuration tests must stay offline"))
        guard.start()
        self.addCleanup(guard.stop)
        self.common = _build_common_cfg(SearchBackendSection(timeout=27, max_results=5, proxy="http://127.0.0.1:7890"))

    def test_anysearch_options_are_passed_exactly_with_parsed_json_parameters(self) -> None:
        params = {"library": "golang", "nested": {"paths": ["fmt", "net/http"]}}
        engines = EnginesSection(
            anysearch_enabled=True,
            anysearch_api_keys=["fixture-only-anysearch-key"],
            anysearch_tag="code.doc",
            anysearch_zone="intl",
            anysearch_language="en-US",
            anysearch_params="  " + json.dumps(params) + "  ",
            anysearch_format="markdown",
        )
        snapshot = deepcopy(self.common)
        self.assertEqual(
            _build_engine_dict("anysearch", engines, self.common),
            {
                **self.common,
                "enabled": True,
                "api_keys": ["fixture-only-anysearch-key"],
                "tag": "code.doc",
                "zone": "intl",
                "language": "en-US",
                "params": params,
                "format": "markdown",
            },
        )
        self.assertEqual(self.common, snapshot)

    def test_exa_options_are_passed_exactly_including_false_and_numeric_values(self) -> None:
        engines = EnginesSection(
            exa_enabled=True,
            exa_api_keys=["fixture-only-exa-key"],
            exa_search_type="deep-lite",
            exa_category="news",
            exa_include_domains=[" example.com ", "example.com", "*.example.org"],
            exa_exclude_domains=[" ads.example.com ", ""],
            exa_start_published_date=" 2026-01-01T00:00:00Z ",
            exa_end_published_date="2026-10-28T23:59:59+08:00",
            exa_user_location=" cn ",
            exa_moderation=True,
            exa_objective="Find official release notes",
            exa_additional_queries=[" release notes ", "", "release notes", "changelog"],
            exa_include_highlights=False,
            exa_highlights_query="Changed behavior",
            exa_highlights_max_characters=800,
            exa_include_text=True,
            exa_text_max_characters=3200,
            exa_max_age_hours=6,
            exa_livecrawl_timeout_ms=1500,
        )
        snapshot = deepcopy(self.common)
        self.assertEqual(
            _build_engine_dict("exa", engines, self.common),
            {
                **self.common,
                "enabled": True,
                "api_keys": ["fixture-only-exa-key"],
                "search_type": "deep-lite",
                "category": "news",
                "include_domains": ["example.com", "*.example.org"],
                "exclude_domains": ["ads.example.com"],
                "start_published_date": "2026-01-01T00:00:00Z",
                "end_published_date": "2026-10-28T23:59:59+08:00",
                "user_location": "CN",
                "moderation": True,
                "objective": "Find official release notes",
                "additional_queries": ["release notes", "changelog"],
                "include_highlights": False,
                "highlights_query": "Changed behavior",
                "highlights_max_characters": 800,
                "include_text": True,
                "text_max_characters": 3200,
                "max_age_hours": 6,
                "livecrawl_timeout_ms": 1500,
            },
        )
        self.assertEqual(self.common, snapshot)

    def test_default_anysearch_options_remain_empty_and_allow_anonymous_configuration(self) -> None:
        self.assertEqual(
            _build_engine_dict("anysearch", EnginesSection(), self.common),
            {
                **self.common,
                "enabled": False,
                "api_keys": [],
                "tag": "",
                "zone": "",
                "language": "",
                "params": {},
                "format": "json",
            },
        )

    def test_default_exa_options_keep_optional_filters_and_content_limits_disabled(self) -> None:
        self.assertEqual(
            _build_engine_dict("exa", EnginesSection(), self.common),
            {
                **self.common,
                "enabled": False,
                "api_keys": [],
                "search_type": "auto",
                "category": "",
                "include_domains": [],
                "exclude_domains": [],
                "start_published_date": "",
                "end_published_date": "",
                "user_location": "",
                "moderation": False,
                "objective": "",
                "additional_queries": [],
                "include_highlights": True,
                "highlights_query": "",
                "highlights_max_characters": 0,
                "include_text": False,
                "text_max_characters": 0,
                "max_age_hours": -2,
                "livecrawl_timeout_ms": 0,
            },
        )

    def test_list_and_nested_parameter_values_are_independent_between_builds_and_models(self) -> None:
        engines = EnginesSection(
            anysearch_api_keys=["fixture-only-anysearch-key"],
            anysearch_params='{"nested":{"paths":["fmt"]}}',
            exa_api_keys=["fixture-only-exa-key"],
            exa_search_type="deep",
            exa_include_domains=["example.com"],
            exa_exclude_domains=["ads.example.com"],
            exa_additional_queries=["follow-up"],
        )
        model_snapshot = engines.model_dump()
        for name, list_fields in (
            ("anysearch", ("api_keys",)),
            ("exa", ("api_keys", "include_domains", "exclude_domains", "additional_queries")),
        ):
            first = _build_engine_dict(name, engines, self.common)
            second = _build_engine_dict(name, engines, self.common)
            for field in list_fields:
                with self.subTest(engine=name, field=field):
                    self.assertIsNot(first[field], second[field])
                    self.assertIsNot(first[field], getattr(engines, f"{name}_{field}"))
                    first[field].append("changed-only-in-first-build")
                    self.assertNotIn("changed-only-in-first-build", second[field])
        first_params = _build_engine_dict("anysearch", engines, self.common)["params"]
        first_params["nested"]["paths"].append("net/http")
        self.assertEqual(
            _build_engine_dict("anysearch", engines, self.common)["params"], {"nested": {"paths": ["fmt"]}}
        )
        self.assertEqual(engines.model_dump(), model_snapshot)

    def test_each_exa_type_is_accepted_and_legacy_or_unknown_types_are_rejected(self) -> None:
        for search_type in ("auto", "lite", "deep-lite", "deep"):
            with self.subTest(search_type=search_type):
                engines = EnginesSection(exa_search_type=search_type)
                self.assertEqual(_build_engine_dict("exa", engines, self.common)["search_type"], search_type)
        for invalid_type in ("fast", "instant", "deep-reasoning", "neural", "unknown", ""):
            with self.subTest(invalid_type=invalid_type), self.assertRaises(ValidationError):
                EnginesSection(exa_search_type=invalid_type)

    def test_anysearch_zone_and_format_choices_are_validated(self) -> None:
        for zone, expected in (("", "auto"), ("auto", "auto"), ("cn", "cn"), ("intl", "intl")):
            with self.subTest(zone=zone):
                self.assertEqual(EnginesSection(anysearch_zone=zone).anysearch_zone, expected)
        for output_format in ("json", "markdown"):
            with self.subTest(output_format=output_format):
                self.assertEqual(EnginesSection(anysearch_format=output_format).anysearch_format, output_format)
        for invalid in ({"anysearch_zone": "us"}, {"anysearch_format": "html"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                EnginesSection(**invalid)

    def test_anysearch_automatic_and_legacy_empty_zones_are_not_sent_to_api(self) -> None:
        for config in ({}, {"anysearch_zone": ""}, {"anysearch_zone": "auto"}):
            with self.subTest(config=config):
                engines = EnginesSection.model_validate(config)
                self.assertEqual(engines.model_dump()["anysearch_zone"], "auto")
                self.assertEqual(_build_engine_dict("anysearch", engines, self.common)["zone"], "")

    def test_anysearch_parameter_json_must_be_an_object_or_empty(self) -> None:
        for source in ("", "  ", "{}", ' {"library":"golang","options":{"strict":true}} '):
            with self.subTest(source=source):
                engines = EnginesSection(anysearch_params=source)
                self.assertEqual(engines.anysearch_params, source.strip())
                self.assertEqual(
                    _build_engine_dict("anysearch", engines, self.common)["params"],
                    json.loads(source) if source.strip() else {},
                )
        for invalid in ("[]", "null", "false", "42", '"string"', "not-json", '{"library":', "{} trailing"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                EnginesSection(anysearch_params=invalid)

    def test_country_code_is_trimmed_uppercased_and_restricted_to_two_ascii_letters(self) -> None:
        for source, expected in ((" cn ", "CN"), ("us", "US"), ("GB", "GB"), ("", ""), ("  ", "")):
            with self.subTest(source=source):
                self.assertEqual(EnginesSection(exa_user_location=source).exa_user_location, expected)
        for invalid in ("USA", "C", "12", "U1", "U S", "\uff23\uff2e", "\u4e2d\u56fd"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                EnginesSection(exa_user_location=invalid)

    def test_publication_dates_accept_iso_values_and_reject_invalid_calendar_values(self) -> None:
        for field in ("exa_start_published_date", "exa_end_published_date"):
            for value in ("", "2026-10-28", "2026-10-28T12:34:56Z", "2026-10-28T12:34:56+08:00"):
                with self.subTest(field=field, value=value):
                    engines = EnginesSection(**{field: " " + value + " "})
                    self.assertEqual(getattr(engines, field), value)
            for invalid in ("not-a-date", "2026-02-30", "2026-13-01", "2026-10-28T25:00:00Z"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    EnginesSection(**{field: invalid})

    def test_numeric_option_boundaries_are_enforced(self) -> None:
        for field, accepted, rejected in (
            ("exa_highlights_max_characters", (0, 1, 1000000), (-1, 1000001)),
            ("exa_text_max_characters", (0, 1, 1000000), (-1, 1000001)),
            ("exa_max_age_hours", (-2, -1, 0, 720), (-3, 721)),
            ("exa_livecrawl_timeout_ms", (0, 1, 90000), (-1, 90001)),
        ):
            for value in accepted:
                with self.subTest(field=field, accepted=value):
                    self.assertEqual(getattr(EnginesSection(**{field: value}), field), value)
            for value in rejected:
                with self.subTest(field=field, rejected=value), self.assertRaises(ValidationError):
                    EnginesSection(**{field: value})

    def test_domain_query_and_objective_size_limits_are_enforced(self) -> None:
        for field in ("exa_include_domains", "exa_exclude_domains"):
            domains = [f"host{index}.example.com" for index in range(1200)]
            with self.subTest(field=field):
                self.assertEqual(getattr(EnginesSection(**{field: domains}), field), domains)
                with self.assertRaises(ValidationError):
                    EnginesSection(**{field: [*domains, "extra.example.com"]})
        queries = [f"query-{index}" for index in range(10)]
        self.assertEqual(
            EnginesSection(exa_search_type="deep", exa_additional_queries=queries).exa_additional_queries, queries
        )
        with self.assertRaises(ValidationError):
            EnginesSection(exa_search_type="deep", exa_additional_queries=[*queries, "extra"])
        self.assertEqual(len(EnginesSection(exa_objective="x" * 4096).exa_objective), 4096)
        with self.assertRaises(ValidationError):
            EnginesSection(exa_objective="x" * 4097)

    def test_filter_lists_are_cleaned_without_mutating_input_lists(self) -> None:
        source = [" first ", "", "first", "second", "  ", "second"]
        snapshot = source[:]
        engines = EnginesSection(
            exa_search_type="deep",
            exa_include_domains=source,
            exa_exclude_domains=source,
            exa_additional_queries=source,
        )
        for field in ("exa_include_domains", "exa_exclude_domains", "exa_additional_queries"):
            with self.subTest(field=field):
                self.assertEqual(getattr(engines, field), ["first", "second"])
                self.assertIsNot(getattr(engines, field), source)
        self.assertEqual(source, snapshot)

    def test_additional_queries_require_deep_modes_but_blank_queries_are_ignored(self) -> None:
        for search_type in ("deep-lite", "deep"):
            with self.subTest(search_type=search_type):
                self.assertEqual(
                    EnginesSection(
                        exa_search_type=search_type, exa_additional_queries=["query"]
                    ).exa_additional_queries,
                    ["query"],
                )
        for search_type in ("auto", "lite"):
            with self.subTest(search_type=search_type):
                with self.assertRaises(ValidationError):
                    EnginesSection(exa_search_type=search_type, exa_additional_queries=["query"])
                self.assertEqual(
                    EnginesSection(
                        exa_search_type=search_type, exa_additional_queries=["", " "]
                    ).exa_additional_queries,
                    [],
                )

    def test_lite_mode_disallows_nondefault_cache_and_livecrawl_options(self) -> None:
        self.assertEqual(EnginesSection(exa_search_type="lite").exa_max_age_hours, -2)
        for overrides in (
            {"exa_max_age_hours": -1},
            {"exa_max_age_hours": 0},
            {"exa_max_age_hours": 1},
            {"exa_livecrawl_timeout_ms": 1},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                EnginesSection(exa_search_type="lite", **overrides)
        for search_type in ("auto", "deep-lite", "deep"):
            with self.subTest(search_type=search_type):
                self.assertEqual(
                    EnginesSection(
                        exa_search_type=search_type, exa_max_age_hours=0, exa_livecrawl_timeout_ms=1000
                    ).exa_livecrawl_timeout_ms,
                    1000,
                )

    def test_company_and_people_categories_reject_dates_and_people_rejects_exclusions(self) -> None:
        for category in ("company", "people", " Company ", " PEOPLE "):
            for field in ("exa_start_published_date", "exa_end_published_date"):
                with self.subTest(category=category, field=field), self.assertRaises(ValidationError):
                    EnginesSection(exa_category=category, **{field: "2026-10-28"})
        for category in ("people", " PEOPLE "):
            with self.subTest(category=category), self.assertRaises(ValidationError):
                EnginesSection(exa_category=category, exa_exclude_domains=["example.com"])
        self.assertEqual(
            EnginesSection(exa_category="company", exa_exclude_domains=["example.com"]).exa_exclude_domains,
            ["example.com"],
        )
        self.assertEqual(
            EnginesSection(
                exa_category="news", exa_start_published_date="2026-10-28", exa_exclude_domains=["example.com"]
            ).exa_start_published_date,
            "2026-10-28",
        )


if __name__ == "__main__":
    unittest.main()
