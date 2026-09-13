"""Keep task routing and request options intact across the SDK boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maibot_sdk.capabilities.llm import LLMCapability

from google_search_plugin.config import ModelsSection
from google_search_plugin.pipelines.llm_runner import LLMCallError, LLMRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("task_name", ["replyer", "utils", "planner", "vlm"])
async def test_generate_routes_task_through_real_sdk(task_name: str):
    rpc = AsyncMock(return_value={"success": True, "response": " summary "})
    ctx = SimpleNamespace(llm=LLMCapability(SimpleNamespace(call_capability=rpc)))
    config = ModelsSection(model_name=task_name, temperature=0.4, llm_timeout_seconds=75)

    assert await LLMRunner(ctx, config).generate("summarize") == "summary"

    rpc.assert_awaited_once_with(
        "llm.generate", prompt="summarize", task_name=task_name, model="", temperature=0.4, timeout_ms=75000
    )


@pytest.mark.asyncio
async def test_generation_failure_still_raises():
    rpc = AsyncMock(return_value={"success": False, "error": "unavailable"})
    ctx = SimpleNamespace(llm=LLMCapability(SimpleNamespace(call_capability=rpc)))

    with pytest.raises(LLMCallError, match="unavailable"):
        await LLMRunner(ctx, ModelsSection()).generate("summarize")
