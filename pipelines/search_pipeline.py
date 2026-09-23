"""主搜索流程:engines → fetch → summarize。

搜索词由调用方(planner 经 web_search Tool 参数)直接给出,插件不再做
LLM 查询重写。工具调用结果由 host 的 maisaka.reasoning_engine 自动写入
``tool_records`` 表,插件本身不写库。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .llm_runner import LLMCallError
from .prompts import build_summarize_prompt, format_results_for_prompt

if TYPE_CHECKING:
    from ..config import SearchBackendSection
    from .content_fetcher import ContentFetcher
    from .engine_chain import EngineChain
    from .llm_runner import LLMRunner

logger = logging.getLogger(__name__)


class SearchPipeline:
    """主搜索流水线"""

    def __init__(
        self,
        *,
        backend_cfg: "SearchBackendSection",
        engine_chain: "EngineChain",
        content_fetcher: "ContentFetcher",
        llm_runner: "LLMRunner",
    ) -> None:
        self._backend = backend_cfg
        self._engines = engine_chain
        self._fetcher = content_fetcher
        self._llm = llm_runner

    async def run(
        self,
        question: str,
        *,
        bot_name: str,
        tavily_topic_override: Optional[str] = None,
    ) -> str:
        """执行主搜索。

        Args:
            question: 搜索关键词或问题(planner 直接给出,原样送引擎)
            bot_name: bot 昵称(prompt 用)
            tavily_topic_override: 调用方显式指定的 tavily topic

        Returns:
            LLM 总结文本;无可用结果时返回提示文本
        """
        # ---- 1. 多引擎 fallback 搜索 ---- #
        max_results = self._backend.max_results
        results = await self._engines.search_with_fallback(
            question,
            max_results,
            tavily_topic=tavily_topic_override,
        )
        if not results:
            return f"关于「{question}」，我没有找到相关的网络信息。"

        # ---- 2. 内容补充(Tavily inline / you_contents / 普通抓取) ---- #
        last_engine = self._engines.last_success_engine or ""
        if last_engine == "tavily":
            self._fetcher.integrate_inline_content(results, self._engines.last_tavily_answer)
        elif self._backend.fetch_content:
            results = await self._fetcher.fetch_batch(results, last_success_engine=last_engine)

        # ---- 3. summarize prompt ---- #
        formatted = format_results_for_prompt(
            results,
            source_max_chars=self._backend.summary_source_max_chars,
            total_max_chars=self._backend.summary_total_max_chars,
        )
        summarize_prompt = build_summarize_prompt(
            bot_name=bot_name,
            question=question,
            formatted_results=formatted,
        )
        logger.info("调用 LLM 对搜索结果进行总结")
        try:
            final_answer = await self._llm.generate(summarize_prompt)
        except LLMCallError as exc:
            logger.warning("summarize LLM 调用失败: %s", exc)
            # 不回显抓取到的 abstract(可能含 PII / 边栏文字),只列搜索引擎元数据
            # (title + url 是公开的搜索结果索引信息,泄漏风险低)
            links = "\n".join(
                f"- {r.title}: {r.url}" for r in results if r.title and r.url
            )
            if links:
                return f"已找到相关结果,但总结服务暂时不可用,可手动查看:\n\n{links}"
            return "搜索服务暂时不可用,请稍后再试。"

        return final_answer
