"""Bing + ContentFetcher 链路质量探测脚本。

只跑插件中的搜索 + 正文抓取层,不走 LLM summarize,也不依赖 maibot_sdk
的运行时。用于离线评估 Bing 在不同类型 query 上的:

  1. 搜索结果数量与命中率(title/url/snippet 解析是否成功)
  2. 正文抓取成功率与内容长度(trafilatura / readability / bs4 三级降级)
  3. 端到端耗时

运行方式(在项目根目录 ``E:\\MaiM-with-u\\MaiBot``):

    python -m plugins.google_search_plugin.tests.bing_quality_probe
    # 或者(自定义 query):
    python -m plugins.google_search_plugin.tests.bing_quality_probe "RTX 5090 跑分" "什么是 MCP 协议"

可选环境变量:
    HTTP_PROXY / HTTPS_PROXY        作为代理传给 Bing 请求
    BING_PROBE_MAX_RESULTS=10       每个 query 取前 N 条
    BING_PROBE_TIMEOUT=20           Bing 搜索超时
    BING_PROBE_CONTENT_TIMEOUT=10   正文抓取超时
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

# Windows 控制台默认 GBK,搜索结果里随便一个 © / — 就把 print 打挂。
# 强制 stdout/stderr 用 UTF-8,遇到无法编码的字符也只是替换成 ?。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# 把项目根目录加入 sys.path,以便用绝对路径导入 plugins.*
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.content_fetcher import ContentFetcher  # noqa: E402
from plugins.google_search_plugin.pipelines.zhihu_extractor import ZhihuExtractor  # noqa: E402
from plugins.google_search_plugin.search_engines.base import SearchResult  # noqa: E402
from plugins.google_search_plugin.search_engines.bing import BingEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.you import YouContentsClient  # noqa: E402

# ---------------------------------------------------------------------------
# 默认覆盖性 query 集
# ---------------------------------------------------------------------------

DEFAULT_QUERIES: List[tuple[str, str]] = [
    ("时效新闻",   "2025 年最新诺贝尔物理学奖得主"),
    ("技术问题",   "asyncio.wait_for 取消任务后如何清理子协程"),
    ("人物百科",   "雷军 小米创始人 简介"),
    ("知乎类问题", "孩子高考志愿应该听家长的还是自己的"),
    ("英文 query", "best practices for python asyncio timeout"),
    ("生僻问法",   "怎样让我家的橘猫不再半夜跳上床"),
]


@dataclass
class QueryReport:
    """单条 query 的探测结果。"""

    category: str
    query: str
    search_elapsed: float
    fetch_elapsed: float
    raw_results: int
    snippet_hits: int
    fetched_count: int
    fetched_total_chars: int
    results: List[SearchResult]


# ---------------------------------------------------------------------------
# 测试装配
# ---------------------------------------------------------------------------


def _build_components() -> tuple[BingEngine, ContentFetcher]:
    """用插件默认配置 + 环境变量 override 构建组件。"""
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    max_results = int(os.environ.get("BING_PROBE_MAX_RESULTS", "10"))
    timeout = int(os.environ.get("BING_PROBE_TIMEOUT", "20"))
    content_timeout = int(os.environ.get("BING_PROBE_CONTENT_TIMEOUT", "10"))

    bing_cfg = {
        "enabled": True,
        "region": "zh-CN",
        "setlang": "zh",
        "count": max_results,
        "timeout": timeout,
        "proxy": proxy or None,
        "max_results": max_results,
    }
    bing = BingEngine(bing_cfg)

    backend_cfg = SearchBackendSection(
        default_engine="bing",
        max_results=max_results,
        timeout=timeout,
        proxy=proxy,
        fetch_content=True,
        content_timeout=content_timeout,
    )
    engines_cfg = EnginesSection()  # 全用默认值即可,我们只走普通页抓取
    zhihu = ZhihuExtractor(
        zhihu_cookies="",
        content_timeout=content_timeout,
        max_content_length=backend_cfg.max_content_length,
        proxy=proxy,
    )
    you_contents = YouContentsClient(
        {
            "enabled": False,
            "api_keys": [],
            "timeout": content_timeout,
            "proxy": proxy or None,
        }
    )
    fetcher = ContentFetcher(
        backend_cfg=backend_cfg,
        engines_cfg=engines_cfg,
        you_contents=you_contents,
        zhihu_extractor=zhihu,
    )
    return bing, fetcher


# ---------------------------------------------------------------------------
# 探测主流程
# ---------------------------------------------------------------------------


async def probe_one(
    bing: BingEngine,
    fetcher: ContentFetcher,
    category: str,
    query: str,
    max_results: int,
) -> QueryReport:
    """跑单条 query 的完整链路(Bing search → ContentFetcher.fetch_batch)。"""
    t0 = time.perf_counter()
    results: List[SearchResult] = await bing.search(query, max_results)
    t1 = time.perf_counter()

    raw_count = len(results)
    snippet_hits = sum(1 for r in results if r.snippet)

    if results:
        results = await fetcher.fetch_batch(results, last_success_engine="bing")
    t2 = time.perf_counter()

    fetched_count = 0
    fetched_total = 0
    for r in results:
        # fetch_batch 会把抓取到的正文拼到 abstract 后面;原 snippet 长度作 baseline
        new_len = len(r.abstract or "")
        if new_len > len(r.snippet or "") + 50:
            fetched_count += 1
            fetched_total += max(0, new_len - len(r.snippet or ""))

    return QueryReport(
        category=category,
        query=query,
        search_elapsed=t1 - t0,
        fetch_elapsed=t2 - t1,
        raw_results=raw_count,
        snippet_hits=snippet_hits,
        fetched_count=fetched_count,
        fetched_total_chars=fetched_total,
        results=results,
    )


# ---------------------------------------------------------------------------
# 报告打印
# ---------------------------------------------------------------------------


def _truncate(text: str, n: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def print_report(report: QueryReport) -> None:
    print("=" * 88)
    print(f"[{report.category}] {report.query}")
    print(
        f"  search={report.search_elapsed:.2f}s  fetch={report.fetch_elapsed:.2f}s  "
        f"results={report.raw_results}  snippet={report.snippet_hits}  "
        f"fetched={report.fetched_count}  +chars={report.fetched_total_chars}"
    )
    if not report.results:
        print("  (无结果)")
        return
    for i, r in enumerate(report.results, 1):
        snip_len = len(r.snippet or "")
        abs_len = len(r.abstract or "")
        delta = abs_len - snip_len
        flag = "+" if delta > 50 else "-"
        print(f"  [{i:02d}] {flag} title={_truncate(r.title, 80)}")
        print(f"        url={r.url}")
        print(f"        snippet({snip_len}c)={_truncate(r.snippet, 110)}")
        if delta > 50:
            extra = (r.abstract or "")[snip_len:]
            print(f"        content(+{delta}c)={_truncate(extra, 140)}")


def print_summary(reports: List[QueryReport]) -> None:
    if not reports:
        return
    print()
    print("#" * 88)
    print("# 汇总")
    print("#" * 88)
    total_raw = sum(r.raw_results for r in reports)
    total_snip = sum(r.snippet_hits for r in reports)
    total_fetched = sum(r.fetched_count for r in reports)
    total_search = sum(r.search_elapsed for r in reports)
    total_fetch = sum(r.fetch_elapsed for r in reports)
    n = len(reports)
    print(
        f"queries={n}  avg_search={total_search / n:.2f}s  avg_fetch={total_fetch / n:.2f}s"
    )
    print(
        f"raw_results={total_raw}  snippet_hits={total_snip} "
        f"({(total_snip / total_raw * 100) if total_raw else 0:.0f}%)  "
        f"fetched={total_fetched} ({(total_fetched / total_raw * 100) if total_raw else 0:.0f}%)"
    )
    print()
    print(f"{'category':<12}{'raw':>5}{'snip':>6}{'fetch':>7}{'tSrch':>8}{'tFetch':>8}  query")
    for r in reports:
        print(
            f"{r.category:<12}{r.raw_results:>5}{r.snippet_hits:>6}{r.fetched_count:>7}"
            f"{r.search_elapsed:>7.2f}s{r.fetch_elapsed:>7.2f}s  {_truncate(r.query, 50)}"
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def main(argv: List[str]) -> int:
    if argv:
        queries = [("custom", q) for q in argv]
    else:
        queries = DEFAULT_QUERIES

    max_results = int(os.environ.get("BING_PROBE_MAX_RESULTS", "10"))
    bing, fetcher = _build_components()

    print(f"BingProbe: {len(queries)} query, max_results={max_results}, proxy={bing.proxy or '(none)'}")
    print()

    reports: List[QueryReport] = []
    for category, query in queries:
        try:
            rep = await probe_one(bing, fetcher, category, query, max_results)
        except Exception as exc:  # noqa: BLE001
            print(f"!! [{category}] {query!r} 出错: {exc}")
            continue
        print_report(rep)
        reports.append(rep)

    print_summary(reports)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
