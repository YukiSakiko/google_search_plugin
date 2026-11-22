"""google_search_plugin 主入口

业务逻辑全部抽到 ``pipelines/`` 子模块,本文件只负责装配 + 派发。

提供四个面向 LLM 的组件:
- ``@Tool("web_search")``             主搜索工具(支持 URL 直访;搜索词由 planner 直接给出)
- ``@Tool("abbreviation_translate")`` 缩写翻译(神奇海螺 nbnhhsh;关闭时置为禁用态,不暴露给 planner)
- ``@Tool("image_search")``           图片搜索(新版 Maisaka Tool 语义,通过 content_items 返回候选图片;关闭时置为禁用态)
- ``@Tool("reverse_image_search")``   以图搜图/识图(支持读取聊天图片 msg_id 或图片 URL,返回关联网页供 planner 总结)

外加一个 ``/google_search_status`` 诊断命令。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, ClassVar, Mapping, Optional

from maibot_sdk import Command, ConfigWidget, ConfigWidgetState, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import GoogleSearchPluginConfig
from .config_ui import build_config_ui_schema
from .pipelines.content_fetcher import ContentFetcher
from .pipelines.engine_chain import EngineChain
from .pipelines.image_search_pipeline import ImageSearchPipeline
from .pipelines.llm_runner import LLMRunner
from .pipelines.public_fetch import MAX_IMAGE_BYTES
from .pipelines.reverse_image_pipeline import ReverseImagePipeline, reverse_rpc_budget
from .pipelines.search_pipeline import SearchPipeline
from .pipelines.url_pipeline import UrlPipeline, is_url
from .pipelines.zhihu_extractor import ZhihuExtractor
from .translators.nbnhhsh import NbnhhshTranslator

ALLOWED_TAVILY_TOPICS = frozenset({"general", "news"})
_API_KEY_PROVIDERS = ("tavily", "you", "deepseek")


class GoogleSearchPlugin(MaiBotPlugin):
    """麦麦联网插件主类"""

    config_model = GoogleSearchPluginConfig

    config_widgets: ClassVar[list[ConfigWidget]] = [
        ConfigWidget(id="google_lens_status", kind="status", label="Google Lens 登录状态"),
        ConfigWidget(
            id="google_lens_login_start",
            kind="action",
            label="打开浏览器登录 Google",
            confirmation="将在运行 MaiBot 的本机弹出浏览器窗口供登录 Google 账号（无桌面环境的服务器请改填 Cookie），是否继续？",
        ),
        ConfigWidget(
            id="google_lens_login_finish",
            kind="action",
            label="完成并保存登录态",
        ),
        ConfigWidget(
            id="google_lens_login_reset",
            kind="action",
            label="清除本地登录态 / 重置冷却",
            confirmation="将清除插件自己创建的默认浏览器资料并重置冷却时间。自定义目录和旧目录不会删除。是否继续？",
        ),
    ]

    @classmethod
    def build_config_schema(cls, **plugin_info: str) -> dict[str, Any]:
        """只重组 WebUI 展示；配置模型和 TOML 路径保持不变。"""
        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    async def get_config_widget_state(self) -> dict[str, ConfigWidgetState]:
        """向 WebUI 配置页上报 Google Lens 实时登录状态与可用按钮。"""
        cfg = self.config
        lens_enabled = bool(cfg.reverse_image.enabled and cfg.reverse_image.google_lens_enabled)
        if not lens_enabled:
            return {
                "google_lens_status": ConfigWidgetState(
                    text="未启用（请先开启「以图搜图」与「启用 Google Lens」，当前仅使用纯 HTTP 引擎或已停用）",
                    tone="neutral",
                ),
                "google_lens_login_start": ConfigWidgetState(disabled=True),
                "google_lens_login_finish": ConfigWidgetState(visible=False, disabled=True),
                "google_lens_login_reset": ConfigWidgetState(disabled=True),
            }

        if not self._ensure_pipelines_ready() or self._reverse_image_pipeline is None:
            return {
                "google_lens_status": ConfigWidgetState(text="以图搜图组件尚未就绪", tone="warning"),
                "google_lens_login_start": ConfigWidgetState(disabled=True),
                "google_lens_login_finish": ConfigWidgetState(visible=False, disabled=True),
                "google_lens_login_reset": ConfigWidgetState(disabled=True),
            }

        snap = self._reverse_image_pipeline.google_lens.get_login_snapshot()
        state = snap["state"]
        active = bool(snap["active"])
        cooldown = int(snap["cooldown_remaining"])
        has_cookies = bool(snap["has_cookies_config"])
        has_profile = bool(snap["has_saved_profile"])
        profile_dir = str(snap["profile_dir"] or "")

        if active:
            status_text = snap["message"] or "浏览器登录窗口运行中，请登录 Google 账号后关闭窗口或点击「完成并保存登录态」。"
            tone = "warning"
        elif state == "error":
            status_text = snap["message"] or "启动登录窗口失败，请检查日志或改填 Cookie。"
            tone = "error"
        elif cooldown > 0:
            status_text = (
                f"当前处于风控冷却期（剩余 {cooldown} 秒），期间自动降级到 Yandex / Bing；"
                "可重新登录 Google 账号或点击清除/重置。"
            )
            tone = "warning"
        elif state == "logged_in" and snap["message"]:
            status_text = snap["message"]
            tone = "success"
        elif has_cookies:
            status_text = snap["message"] or "已填写 Google 登录信息，尚未验证账号状态。"
            tone = "neutral"
        elif has_profile:
            status_text = snap["message"] or f"已找到本地浏览器资料，尚未确认账号登录状态：{profile_dir}"
            tone = "neutral"
        else:
            status_text = (
                snap["message"]
                or "尚未登录本地 Google 账号（未登录时可能触发验证码并自动降级至 Yandex / Bing）；"
                "桌面端可直接点击下方「打开浏览器登录 Google」。"
            )
            tone = "neutral"

        return {
            "google_lens_status": ConfigWidgetState(text=status_text, tone=tone),
            "google_lens_login_start": ConfigWidgetState(disabled=active),
            "google_lens_login_finish": ConfigWidgetState(visible=active, disabled=(state == "saving")),
            "google_lens_login_reset": ConfigWidgetState(
                disabled=active or (not has_profile and cooldown == 0 and state != "error")
            ),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        """处理 WebUI 配置页的 Google Lens 登录/保存/清除按钮点击。"""
        if not self._ensure_pipelines_ready() or self._reverse_image_pipeline is None:
            raise RuntimeError("以图搜图组件尚未就绪")
        lens = self._reverse_image_pipeline.google_lens
        if action_id == "google_lens_login_start":
            await lens.start_interactive_login()
        elif action_id == "google_lens_login_finish":
            await lens.finish_interactive_login()
        elif action_id == "google_lens_login_reset":
            await lens.reset_login_state()
        else:
            raise ValueError(f"未知配置页操作: {action_id}")

    def get_default_config(self) -> dict[str, Any]:
        """保留旧字段直到插件归一化，避免宿主升级重建时提前丢弃密钥。"""
        # 只扩展宿主使用的实例入口；SDK 归一化仍使用干净的 build_default_config()。
        defaults = super().get_default_config()
        for provider in _API_KEY_PROVIDERS:
            defaults["engines"][f"{provider}_api_key"] = ""
        return defaults

    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """将旧单密钥一次性并入列表，输出始终遵循当前配置模型。"""
        raw_config = dict(config_data) if config_data is not None else {}
        source_engines = raw_config.get("engines")
        if not isinstance(source_engines, Mapping):
            return super().normalize_plugin_config(config_data)

        engines = dict(source_engines)
        for provider in _API_KEY_PROVIDERS:
            old_field = f"{provider}_api_key"
            old_key = engines.pop(old_field, "")
            if not isinstance(old_key, str):
                raise ValueError(f"engines.{old_field} 必须是字符串")
            old_key = old_key.strip()
            if not old_key:
                continue
            list_field = f"{provider}_api_keys"
            keys = engines.get(list_field, [])
            if not isinstance(keys, list):
                raise ValueError(f"engines.{list_field} 必须是密钥列表")
            if not any(isinstance(key, str) and key.strip() == old_key for key in keys):
                engines[list_field] = [*keys, old_key]
        raw_config["engines"] = engines
        normalized, changed = super().normalize_plugin_config(raw_config)
        return normalized, changed or normalized != config_data

    # 运行时组件:在 on_load / on_config_update 中装配
    _engine_chain: Optional[EngineChain]
    _content_fetcher: Optional[ContentFetcher]
    _llm_runner: Optional[LLMRunner]
    _search_pipeline: Optional[SearchPipeline]
    _url_pipeline: Optional[UrlPipeline]
    _translator: Optional[NbnhhshTranslator]
    _image_pipeline: Optional[ImageSearchPipeline]
    _reverse_image_pipeline: Optional[ReverseImagePipeline]

    def __init__(self) -> None:
        super().__init__()
        self._engine_chain = None
        self._content_fetcher = None
        self._llm_runner = None
        self._search_pipeline = None
        self._url_pipeline = None
        self._translator = None
        self._image_pipeline = None
        self._reverse_image_pipeline = None

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #

    async def _close_reverse_image_pipeline(self) -> None:
        pipeline = self._reverse_image_pipeline
        if pipeline is not None:
            try:
                await pipeline.aclose()
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("关闭以图搜图浏览器资源失败: %s", exc)

    async def on_load(self) -> None:
        self._build_pipelines()
        cfg = self.config
        self.ctx.logger.info(
            "google_search_plugin v%s 已加载 (model=%s, default_engine=%s, image_search=%s, reverse_image=%s, translation=%s)",
            cfg.plugin.version,
            cfg.models.model_name,
            cfg.search_backend.default_engine,
            cfg.actions.image_search_enabled,
            cfg.reverse_image.enabled,
            cfg.translation.enabled,
        )

    async def on_unload(self) -> None:
        await self._close_reverse_image_pipeline()
        self.ctx.logger.info("google_search_plugin 已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        """配置热更新:先释放旧浏览器资源，再重建所有组件。"""
        del config_data
        self.ctx.logger.info("配置更新事件: scope=%s version=%s,重建 pipelines", scope, version)
        await self._close_reverse_image_pipeline()
        try:
            self._build_pipelines()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("重建 pipelines 失败: %s", exc, exc_info=True)
        await self._sync_component_states()

    def _configured_component_states(self) -> dict[str, tuple[str, bool]]:
        """按当前配置给出可开关组件的期望状态: name -> (component_type, enabled)。"""
        cfg = self.config
        return {
            "abbreviation_translate": ("tool", bool(cfg.translation.enabled)),
            "image_search": ("tool", bool(cfg.actions.image_search_enabled)),
            "reverse_image_search": ("tool", bool(cfg.reverse_image.enabled)),
        }

    def get_components(self) -> list[dict[str, Any]]:
        """收集组件声明,按配置写入初始启用态,并注入长耗时 Tool 的 RPC 超时。

        关闭功能时不能直接从声明中剔除对应组件:组件不进注册表的话,
        运行时热启用会因"未找到组件"失败,只能重载插件恢复。
        因此保持注册、仅置为禁用态,使其不暴露给 planner。

        timeout_ms 必须写在声明顶层:Host 侧只读 metadata 顶层的
        ``timeout_ms``(缺省落 DEFAULT_COMPONENT_RPC_TIMEOUT_MS=60s),
        而 ``@Tool`` 装饰器的额外 kwargs 会落入嵌套 metadata 不被读取;
        慢模型下 summarize 单次即可超过 60s,整个 plugin.invoke_tool 会被截断。
        """
        components = super().get_components()
        try:
            states = self._configured_component_states()
        except Exception:  # noqa: BLE001
            states = {}
        web_search_timeout_ms = self._web_search_rpc_timeout_ms()
        reverse_image_timeout_ms = self._reverse_image_rpc_timeout_ms()
        for comp in components:
            name = comp.get("name", "")
            metadata = comp.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if name == "web_search":
                metadata["timeout_ms"] = web_search_timeout_ms
            elif name == "reverse_image_search":
                metadata["timeout_ms"] = reverse_image_timeout_ms
            state = states.get(name)
            if state is not None:
                metadata["enabled"] = state[1]
        return components

    _WEB_SEARCH_RPC_FLOOR_SECONDS = 180
    _WEB_SEARCH_NON_LLM_BUDGET_SECONDS = 120

    def _web_search_rpc_timeout_ms(self) -> int:
        """整体超时 = 单次 LLM 上限 + 搜索/抓取预算,下限 180s。"""
        try:
            llm_timeout = int(self.config.models.llm_timeout_seconds or 60)
        except Exception:  # noqa: BLE001  # 配置尚未注入时按默认值兜底
            llm_timeout = 60
        total_seconds = max(
            self._WEB_SEARCH_RPC_FLOOR_SECONDS,
            llm_timeout + self._WEB_SEARCH_NON_LLM_BUDGET_SECONDS,
        )
        return total_seconds * 1000

    def _reverse_image_rpc_timeout_ms(self) -> int:
        """为已启用引擎的串行回退、读图和返回结果留足时间。"""
        try:
            cfg = self.config
        except RuntimeError:
            cfg = GoogleSearchPluginConfig()
        return reverse_rpc_budget(cfg.reverse_image, cfg.search_backend) * 1000

    async def _sync_component_states(self) -> None:
        """按当前配置热切换可开关组件的启用状态。

        宿主侧 enable/disable 仅翻转内存态,重启后以注册元数据
        (见 ``get_components``)为准,两者互补无需持久化。
        """
        for name, (component_type, enabled) in self._configured_component_states().items():
            try:
                if enabled:
                    result = await self.ctx.component.enable_component(name, component_type)
                else:
                    result = await self.ctx.component.disable_component(name, component_type)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("切换 %s 启用状态失败: %s", name, exc)
                continue
            if isinstance(result, dict) and not result.get("success", False):
                self.ctx.logger.warning(
                    "切换 %s 启用状态被拒绝: %s",
                    name,
                    result.get("error", "未知原因"),
                )
            else:
                self.ctx.logger.info("%s 组件已%s", name, "启用" if enabled else "禁用")

    def _build_pipelines(self) -> None:
        """从 self.config 装配所有运行时组件。"""
        cfg = self.config
        self._engine_chain = EngineChain(cfg.engines, cfg.search_backend)
        zhihu = ZhihuExtractor(
            zhihu_cookies=cfg.search_backend.zhihu_cookies,
            content_timeout=cfg.search_backend.content_timeout,
            max_content_length=cfg.search_backend.max_content_length,
            proxy=cfg.search_backend.proxy or "",
        )
        self._content_fetcher = ContentFetcher(
            backend_cfg=cfg.search_backend,
            engines_cfg=cfg.engines,
            you_contents=self._engine_chain.you_contents,
            zhihu_extractor=zhihu,
        )
        self._llm_runner = LLMRunner(self.ctx, cfg.models)
        self._search_pipeline = SearchPipeline(
            backend_cfg=cfg.search_backend,
            engine_chain=self._engine_chain,
            content_fetcher=self._content_fetcher,
            llm_runner=self._llm_runner,
        )
        self._url_pipeline = UrlPipeline(
            content_fetcher=self._content_fetcher,
            llm_runner=self._llm_runner,
        )
        # 翻译器(NbnhhshTranslator 接 dict 配置)
        self._translator = NbnhhshTranslator(
            {
                "timeout": cfg.translation.timeout_seconds,
                "max_retries": cfg.translation.max_retries,
            }
        )
        # 图片搜索 pipeline
        self._image_pipeline = ImageSearchPipeline(
            engines_cfg=cfg.engines,
            backend_cfg=cfg.search_backend,
        )
        # 反向以图搜图 pipeline
        self._reverse_image_pipeline = ReverseImagePipeline(
            reverse_cfg=cfg.reverse_image,
            backend_cfg=cfg.search_backend,
            content_fetcher=self._content_fetcher,
            google_language=cfg.engines.google_language,
        )

    async def _resolve_bot_name(self) -> str:
        """从全局 bot 配置取昵称(失败时兜底 '机器人')。"""
        try:
            value = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("config.get bot.nickname 失败: %s", exc)
            value = ""
        return str(value).strip() or "机器人"

    def _ensure_pipelines_ready(self) -> bool:
        """确保 pipelines 已装配;未装配则尝试重建。"""
        if all(
            v is not None
            for v in (
                self._engine_chain,
                self._content_fetcher,
                self._llm_runner,
                self._search_pipeline,
                self._url_pipeline,
                self._translator,
                self._image_pipeline,
                self._reverse_image_pipeline,
            )
        ):
            return True
        self.ctx.logger.warning("pipelines 未就绪,尝试重建")
        try:
            self._build_pipelines()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("pipelines 重建失败: %s", exc, exc_info=True)
            return False
        return True

    # ---------------------------------------------------------------- #
    # Tool: web_search
    # ---------------------------------------------------------------- #

    @Tool(
        "web_search",
        description="谷歌搜索工具。当见到有人发出疑问或者遇到不熟悉的事情时候，直接使用它获得最新知识！",
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用于搜索引擎的搜索关键词或完整问题(将被原样送入搜索引擎),也可直接传入 URL 获取网页内容",
                required=True,
            ),
            ToolParameterInfo(
                name="tavily_topic",
                param_type=ToolParamType.STRING,
                description=(
                    "可选:Tavily topic 覆写(general/news);留空表示不传 topic。"
                    "中文场景不建议指定 news,Tavily news 索引偏向英文体育/政治新闻。"
                ),
                required=False,
            ),
        ],
    )
    async def handle_web_search(
        self,
        question: str = "",
        tavily_topic: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """主搜索入口。"""
        # 兼容模型常用的 query 参数名（OpenAI/Grok/Tavily 等通用约定）
        if not (question or "").strip():
            for alias in ("query", "q", "search_query", "keyword"):
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    question = value
                    break
        del kwargs

        question = (question or "").strip()
        if not question:
            return {"name": "web_search", "content": "问题为空，无法执行搜索。"}

        if not self._ensure_pipelines_ready():
            return {"name": "web_search", "content": ""}

        # tavily_topic 校验
        normalized_topic = (tavily_topic or "").strip().lower()
        topic_override = normalized_topic if normalized_topic in ALLOWED_TAVILY_TOPICS else None

        bot_name = await self._resolve_bot_name()

        try:
            if is_url(question):
                self.ctx.logger.info("检测到 URL 输入,直接访问并总结: %s", question)
                content = await self._url_pipeline.run(  # type: ignore[union-attr]
                    question,
                    bot_name=bot_name,
                )
            else:
                self.ctx.logger.info("开始执行搜索: %s", question)
                content = await self._search_pipeline.run(  # type: ignore[union-attr]
                    question,
                    bot_name=bot_name,
                    tavily_topic_override=topic_override,
                )
            return {"name": "web_search", "content": content}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("web_search 执行异常: %s", exc, exc_info=True)
            return {"name": "web_search", "content": ""}

    # ---------------------------------------------------------------- #
    # Tool: abbreviation_translate
    # ---------------------------------------------------------------- #

    @Tool(
        "abbreviation_translate",
        description=(
            "当遇到用户消息中出现难懂的网络用语、缩写、黑话、热词或流行语时，"
            "主动查询并翻译这些词汇以帮助理解。适用于各种类型的网络语言，"
            "包括字母缩写（如yyds、u1s1）、网络黑话、当下热词、流行语等。"
            "应该识别消息中可能让人困惑的网络用语并自动查询其含义。"
        ),
        parameters=[
            ToolParameterInfo(
                name="term",
                param_type=ToolParamType.STRING,
                description="从用户消息中识别出的网络用语、缩写或热词（如：yyds、躺平、内卷等）",
                required=True,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="返回翻译结果数量,默认 3",
                required=False,
            ),
        ],
    )
    async def handle_abbreviation_translate(
        self,
        term: str = "",
        max_results: int = 3,
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """缩写翻译入口(走 nbnhhsh 神奇海螺 API)。"""
        del kwargs
        del stream_id  # 翻译结果通过 return content 返回给 LLM,不直接 send

        if not self.config.translation.enabled:
            return {"name": "abbreviation_translate", "content": "翻译功能已禁用"}

        term = (term or "").strip()
        if not term:
            return {"name": "abbreviation_translate", "content": "未提供要翻译的词汇"}

        if not self._ensure_pipelines_ready():
            return {"name": "abbreviation_translate", "content": "翻译组件未就绪"}

        try:
            self.ctx.logger.info("翻译: %s", term)
            result = await self._translator.translate(term)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("翻译异常: %s", exc, exc_info=True)
            return {"name": "abbreviation_translate", "content": f"缩写翻译失败: {exc}"}

        translations = result.translations[:max_results] if result.translations else []
        if not translations:
            return {"name": "abbreviation_translate", "content": f"未找到「{term}」的翻译结果"}

        if len(translations) == 1:
            content = f"网络用语「{term}」的含义是：{translations[0]}"
        else:
            lines = "\n".join(f"• {t}" for t in translations)
            content = f"网络用语「{term}」的可能含义：\n{lines}"
        return {"name": "abbreviation_translate", "content": content}

    # ---------------------------------------------------------------- #
    # Tool: image_search (已从弃用的 @Action 迁移为新版 Maisaka @Tool)
    # ---------------------------------------------------------------- #

    @Tool(
        "image_search",
        description=(
            "按关键词搜索候选图片（例如用户说“搜一下猫的图片”、“来张风景图”）。"
            "仅在用户明确表示想看、想找或想要某类图片时调用；普通聊天提到某事物时不应使用。"
            "工具会返回一张候选图片媒体（30 分钟内同关键词不重复），"
            "如需发送给用户，请在 reply 工具的 attach_pic 中引用本次返回的 media_index。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="需要搜索的图片关键词",
                required=True,
            ),
        ],
    )
    async def handle_image_search(
        self,
        query: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """图片搜索入口（新版 Maisaka Tool 语义：通过 content_items 返回候选图片）。"""
        if not (query or "").strip():
            for alias in ("keyword", "q", "search_query"):
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    query = value
                    break
        del kwargs

        if not self.config.actions.image_search_enabled:
            return {"name": "image_search", "success": False, "content": "图片搜索功能未启用"}

        query = (query or "").strip()
        if not query:
            return {"name": "image_search", "success": False, "content": "关键词为空，无法搜索图片。"}

        if not self._ensure_pipelines_ready():
            return {"name": "image_search", "success": False, "content": "图片搜索组件未就绪"}

        try:
            self.ctx.logger.info("开始图片搜索: %s", query)
            status, b64, url, mime_type = await self._image_pipeline.find_unique_image_media(  # type: ignore[union-attr]
                query
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("图片搜索工具异常: %s", exc, exc_info=True)
            return {"name": "image_search", "success": False, "content": f"图片搜索失败：{exc}"}

        if status == "ok" and b64 and mime_type:
            effective_mime = mime_type
            ext = effective_mime.split("/", 1)[-1].replace("jpeg", "jpg") or "jpg"
            self.ctx.logger.info("图片搜索成功 url=%s mime=%s", url, effective_mime)
            return {
                "name": "image_search",
                "success": True,
                "content": (
                    f"已为「{query}」找到一张候选图片（来源：{url or '未知'}）。"
                    "如果确认图片合适并需要发送给用户，请在 reply 工具的 attach_pic 中填写本次返回的 media_index。"
                ),
                "content_items": [
                    {
                        "content_type": "image",
                        "data": b64,
                        "mime_type": effective_mime,
                        "name": f"image_search.{ext}",
                        "description": f"关键词「{query}」的搜索结果图片",
                        "metadata": {"source_url": url or ""},
                    }
                ],
            }

        if status == "no_results":
            return {"name": "image_search", "success": False, "content": f"没有找到关于「{query}」的图片。"}

        if status == "no_unique":
            return {
                "name": "image_search",
                "success": False,
                "content": f"最近 30 分钟内已经返回过关于「{query}」的候选图片，暂无不重复的新图。",
            }

        return {
            "name": "image_search",
            "success": False,
            "content": f"找到了关于「{query}」的候选图片，但下载全部失败，可能是网络问题。",
        }

    # ---------------------------------------------------------------- #
    # Tool: reverse_image_search (以图搜图 / 识图)
    # ---------------------------------------------------------------- #

    @Tool(
        "reverse_image_search",
        description=(
            "以图搜图/识图工具。当聊天中出现不认识的角色、表情包、二创图、梗图、截图、画师作品或物体，"
            "想要查找图片出处、角色身份、网络梗背景或关联网页信息时调用。"
            "可传入聊天图片所在消息的 msg_id（以及可选图片序号 image_index），或直接传入图片链接 image_url。"
            "优先返回 Google Lens 页面上的 AI 概览，也可能只有关联网页。可以根据来源总结，但不要把自己的推断说成 Google 的概览。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description=(
                    "可选：待识别图片所在的上下文消息编号（支持短编号）或工具媒体索引 "
                    "tool_result:<call_id>:<item_index>；与 image_url 二选一。"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="image_index",
                param_type=ToolParamType.INTEGER,
                description="可选：同一条消息中的图片序号（从 0 开始，默认 0）。",
                required=False,
            ),
            ToolParameterInfo(
                name="image_url",
                param_type=ToolParamType.STRING,
                description="可选：待识别图片的公网 HTTP/HTTPS 链接；与 msg_id 二选一。",
                required=False,
            ),
        ],
    )
    async def handle_reverse_image_search(
        self,
        msg_id: str = "",
        image_index: int = 0,
        image_url: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """以图搜图入口（优先读取 Maisaka 上下文图片，走 Google Lens → Yandex → Bing）。"""
        source_id = (msg_id or "").strip()
        if not source_id:
            for alias in ("source_id", "media_index", "message_id"):
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    source_id = value.strip()
                    break

        if not (image_url or "").strip():
            for alias in ("url", "img_url"):
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    image_url = value.strip()
                    break

        if image_index == 0 and "index" in kwargs and isinstance(kwargs.get("index"), int):
            image_index = kwargs["index"]
        del kwargs

        def failure(message: str) -> dict[str, Any]:
            return {"name": "reverse_image_search", "success": False, "content": message, "error": message}

        if not self.config.reverse_image.enabled:
            return failure("以图搜图功能未启用")

        if not isinstance(image_index, int) or isinstance(image_index, bool) or image_index < 0:
            return failure("图片序号必须是非负整数。")

        cleaned_url = (image_url or "").strip()
        if source_id and cleaned_url:
            return failure("聊天图片和图片链接只能选一个。")
        if not source_id and not cleaned_url:
            return failure("请提供图片所在的消息编号或图片链接。")

        if not self._ensure_pipelines_ready():
            return failure("以图搜图组件未就绪")

        started = time.perf_counter()
        image_bytes: Optional[bytes] = None
        image_hash = ""

        if source_id:
            normalized_stream_id = (stream_id or "").strip()
            if not normalized_stream_id:
                return failure("缺少当前会话信息，无法按消息编号读取图片。")
            try:
                resolved = await self.ctx.maisaka.context.resolve_image(
                    normalized_stream_id,
                    source_id,
                    index=image_index,
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error("读取上下文图片失败: %s", exc, exc_info=True)
                return failure("读取聊天图片失败，请查看插件日志。")
            if not isinstance(resolved, dict) or not resolved.get("success"):
                err = resolved.get("error", "未知错误") if isinstance(resolved, dict) else str(resolved)
                return failure(f"无法获取目标图片（{err}）。")
            image_payload = resolved.get("image") or {}
            if not isinstance(image_payload, dict):
                return failure("宿主返回的图片数据无效。")
            raw_b64 = str(image_payload.get("binary_data_base64") or "").strip()
            image_hash = str(image_payload.get("binary_hash") or "").strip()
            if not raw_b64:
                return failure("目标图片数据为空，无法识别。")
            if len(raw_b64) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                return failure("图片超过 15 MiB。")
            try:
                image_bytes = base64.b64decode(raw_b64, validate=True)
            except ValueError:
                return failure("无法解码宿主返回的图片数据。")

        try:
            self.ctx.logger.info(
                "开始以图搜图（%s，准备输入 %.3f 秒）",
                "聊天图片" if source_id else "图片链接",
                time.perf_counter() - started,
            )
            content = await self._reverse_image_pipeline.run(  # type: ignore[union-attr]
                image_bytes=image_bytes,
                image_url=cleaned_url,
                image_hash=image_hash,
            )
            return {"name": "reverse_image_search", "success": True, "content": content}
        except ValueError as exc:
            return failure(f"以图搜图失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("reverse_image_search 执行异常: %s", exc, exc_info=True)
            return failure("以图搜图执行失败，请查看插件日志。")
        finally:
            self.ctx.logger.info("以图搜图调用结束，总耗时 %.3f 秒", time.perf_counter() - started)

    # ---------------------------------------------------------------- #
    # 诊断命令
    # ---------------------------------------------------------------- #

    @Command(
        "google_search_status",
        description="查询 google_search_plugin 当前加载状态与关键配置",
        pattern=r"^/google_search_status\s*$",
    )
    async def handle_status(
        self,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs

        cfg = self.config
        e = cfg.engines
        enabled_engines = []
        if e.google_enabled:
            enabled_engines.append("google")
        if e.bing_enabled:
            enabled_engines.append("bing")
        if e.sogou_enabled:
            enabled_engines.append("sogou")
        if e.duckduckgo_enabled:
            enabled_engines.append("duckduckgo")
        if e.tavily_enabled:
            enabled_engines.append("tavily")
        if e.anysearch_enabled:
            enabled_engines.append("anysearch")
        if e.exa_enabled:
            enabled_engines.append("exa")
        if e.keenable_enabled:
            enabled_engines.append("keenable")
        if e.you_enabled:
            enabled_engines.append("you")
        if e.you_news_enabled:
            enabled_engines.append("you_news")
        if e.deepseek_enabled:
            enabled_engines.append("deepseek")

        ri = cfg.reverse_image
        enabled_ri_engines = []
        if ri.google_lens_enabled:
            enabled_ri_engines.append("google_lens")
        if ri.yandex_enabled:
            enabled_ri_engines.append("yandex")
        if ri.bing_enabled:
            enabled_ri_engines.append("bing")

        ready = all(
            v is not None
            for v in (
                self._engine_chain,
                self._content_fetcher,
                self._llm_runner,
                self._search_pipeline,
                self._url_pipeline,
                self._translator,
                self._image_pipeline,
                self._reverse_image_pipeline,
            )
        )

        ri_status = (
            f"已启用 ({', '.join(enabled_ri_engines) if enabled_ri_engines else '无可用子引擎'})"
            if ri.enabled
            else "未启用"
        )
        lines = [
            f"google_search_plugin v{cfg.plugin.version}",
            f"模型 task: {cfg.models.model_name}  温度: {cfg.models.temperature}",
            f"默认引擎: {cfg.search_backend.default_engine}",
            f"启用引擎: {', '.join(enabled_engines) if enabled_engines else '(无)'}",
            f"图片搜索: {'已启用' if cfg.actions.image_search_enabled else '未启用'}",
            f"以图搜图: {ri_status}",
            f"缩写翻译: {'已启用' if cfg.translation.enabled else '未启用'}",
            f"组件就绪: {'是' if ready else '否'}",
        ]
        message = "\n".join(lines)

        if stream_id:
            await self.ctx.send.text(message, stream_id)
        return True, message, True


def create_plugin() -> GoogleSearchPlugin:
    """Runner 通过此工厂函数实例化插件"""
    return GoogleSearchPlugin()


_logger = logging.getLogger(__name__)
