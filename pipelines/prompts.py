"""LLM 提示模板。

设计原则:这些是**纯函数**,bot_name / 时间等动态信息由调用方(SearchPipeline)
提前 resolve 后传入,prompts 模块本身不依赖 ctx。
"""

from __future__ import annotations

import textwrap
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..search_engines.base import SearchResult


def _identity_header(bot_name: str) -> str:
    """提供给 LLM 的身份与时间提示,降低时间误判。

    Args:
        bot_name: bot 昵称(由调用方从 ctx.config.get 取)
    """
    name = (bot_name or "机器人").strip() or "机器人"
    time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return f"你的名字是{name}。现在是{time_now}。"


def build_summarize_prompt(
    *,
    bot_name: str,
    question: str,
    formatted_results: str,
) -> str:
    """构建搜索结果总结 prompt"""
    return textwrap.dedent(
        f"""
        {_identity_header(bot_name)}
        [任务]
        你是一个专业的网络信息整合专家。你的任务是根据搜索问题和一系列从互联网上搜索到的资料，给出一个全面、准确、简洁的回答。

        [搜索问题]
        {question}

        [搜索到的资料]
        {formatted_results}

        [要求]
        1.  仔细阅读所有资料，并围绕搜索问题进行回答。
        2.  答案应该自然流畅，像是你自己总结的，而不是简单的资料拼接。
        3.  如果资料中有相互矛盾的信息，请客观地指出来。
        4.  如果资料不足以回答问题，请诚实地说明。
        5.  新闻或实时信息可能比模型训练时间新，不要因为时间新就认为是虚构内容。
        6.  不要在回答中提及你查阅了资料，直接给出答案。

        [你的回答]
        """
    ).strip()


def build_url_summarize_prompt(*, bot_name: str, url: str, content: str) -> str:
    """构建 URL 直访总结 prompt"""
    truncated_content = (content or "")[:8000]
    return textwrap.dedent(
        f"""
        {_identity_header(bot_name)}
        [任务]
        你是一个专业的内容总结专家。用户提供了一个网页链接，你的任务是阅读这个网页的内容，并提供一个全面、准确、结构清晰的总结。

        [网页URL]
        {url}

        [网页内容]
        {truncated_content}

        [要求]
        1. 提供网页的主要内容概述
        2. 如果是文章，总结其核心观点和关键信息
        3. 如果是产品页面，说明产品的主要特性和用途
        4. 如果是新闻，说明事件的关键要素（何时、何地、何人、何事、为何）
        5. 保持客观中立，不要添加主观评价
        6. 使用清晰的结构和层次组织信息
        7. 不要因为发布时间较新就认为内容是虚构的，请按当前时间理解信息
        8. 如果内容过于简短或无实质信息，请说明

        [你的总结]
        """
    ).strip()


def format_results_for_prompt(
    results: "list[SearchResult]",
    *,
    source_max_chars: int,
    total_max_chars: int,
) -> str:
    """格式化搜索结果用于 summarize prompt 的 ``[搜索到的资料]`` 段。

    Args:
        results: 搜索结果列表
    """
    lines: list[str] = []
    total_chars = 0
    for idx, result in enumerate(results, start=1):
        if total_chars >= total_max_chars:
            break

        header = f"{idx}. {result.title}"
        if result.url:
            header += f" {result.url}"
        lines.append(header)

        remaining_chars = min(source_max_chars, total_max_chars - total_chars)
        abstract = (result.abstract or result.snippet or "").strip()
        content = (result.content or "").strip()
        if abstract:
            abstract_excerpt = abstract[:remaining_chars]
            lines.append(f"摘要：{abstract_excerpt}")
            total_chars += len(abstract_excerpt)
            remaining_chars -= len(abstract_excerpt)

        # Tavily 的 raw_content 会写入 content；仅在它不同于摘要且仍有预算时附带正文。
        if content and content != abstract and remaining_chars > 0:
            content_excerpt = content[:remaining_chars]
            lines.append(f"正文摘录：{content_excerpt}")
            total_chars += len(content_excerpt)
        lines.append("")
    return "\n".join(lines).strip()
