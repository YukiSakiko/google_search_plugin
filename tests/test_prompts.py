"""提示词结果格式化测试。"""

from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipelines.prompts import format_results_for_prompt
from search_engines.base import SearchResult


def test_format_results_limits_raw_content_per_source() -> None:
    """原始正文应计入单来源字符上限，避免提示词无限增长。"""
    result = SearchResult(
        title="示例来源",
        url="https://example.com",
        snippet="abc",
        abstract="abc",
        content="x" * 100,
    )

    formatted = format_results_for_prompt(
        [result],
        source_max_chars=10,
        total_max_chars=10,
    )

    assert "摘要：abc" in formatted
    assert "正文摘录：xxxxxxx" in formatted
    assert "正文摘录：xxxxxxxx" not in formatted
