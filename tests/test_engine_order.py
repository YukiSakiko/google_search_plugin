"""Configuration contract for the search-engine list and legacy default."""

from pathlib import Path
import sys
import unittest

from pydantic import ValidationError

_HOST_ROOT = Path(__file__).resolve().parents[3]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from plugins.google_search_plugin.config import SearchBackendSection  # noqa: E402


class SearchEngineOrderConfigTests(unittest.TestCase):
    def test_missing_order_promotes_legacy_default_without_changing_the_other_positions(self) -> None:
        fallback = [
            "deepseek",
            "tavily",
            "webz",
            "anysearch",
            "exa",
            "keenable",
            "you",
            "you_news",
            "google",
            "bing",
            "duckduckgo",
            "sogou",
        ]
        for engine in fallback:
            with self.subTest(engine=engine):
                old = SearchBackendSection.model_validate({"default_engine": engine})
                self.assertEqual(old.engine_ids, [engine] + [name for name in fallback if name != engine])
        self.assertEqual(SearchBackendSection().engine_ids, ["bing"] + [name for name in fallback if name != "bing"])

    def test_explicit_order_is_deduplicated_without_reinserting_missing_engines(self) -> None:
        for order, expected in (([], []), (["sogou", "bing", "sogou"], ["sogou", "bing"])):
            with self.subTest(order=order):
                backend = SearchBackendSection.model_validate({"default_engine": "tavily", "engine_order": order})
                self.assertEqual(backend.engine_ids, expected)
                self.assertEqual(SearchBackendSection.model_validate(backend.model_dump()).engine_ids, expected)

    def test_saved_rows_use_display_labels_and_keep_their_order(self) -> None:
        rows = [{"engine": "Webz.io"}, {"engine": "You 搜索"}, {"engine": "You 新闻"}]
        backend = SearchBackendSection.model_validate({"engine_order": [*rows, rows[0]]})
        self.assertEqual(backend.engine_ids, ["webz", "you", "you_news"])
        self.assertEqual(backend.model_dump()["engine_order"], rows)

    def test_unknown_engine_is_rejected_instead_of_silently_becoming_a_fallback(self) -> None:
        for unknown in ("unknown_engine", {"engine": "unknown_engine"}):
            with self.subTest(unknown=unknown), self.assertRaises(ValidationError):
                SearchBackendSection.model_validate({"engine_order": [unknown]})
