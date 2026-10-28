"""google_search_plugin 配置模型。

按 PluginConfigBase 拆分为多个 section。注意 ``[models]`` section 不能叫
``model_config``——Pydantic v2 把这个名字保留给 BaseModel 的元数据属性。
"""

import json
from datetime import datetime
from typing import Literal

from pydantic import field_validator, model_validator

from maibot_sdk import Field, PluginConfigBase

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础信息"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    name: str = Field(default="google_search", description="插件名称")
    version: str = Field(default="4.0.3", description="插件版本")
    config_version: str = Field(default="4.2.1", description="配置版本(Runner 用于兼容性校验)")
    enabled: bool = Field(default=True, description="是否启用插件")


class ModelsSection(PluginConfigBase):
    """搜索/总结使用的 LLM 任务参数。

    section 名为 ``models`` 而非 ``model_config``——后者被 Pydantic v2 保留为
    BaseModel 的元数据属性,不能用作字段名。
    """

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 1

    model_name: Literal[
        "replyer",
        "utils",
        "planner",
        "vlm",
    ] = Field(
        default="replyer",
        description=(
            "指定用于搜索和总结的系统模型 task。可选值与 host model_configs.py "
            "里的 chat 类 task 对齐(replyer/utils/planner/vlm),默认 'replyer'。"
        ),
    )
    temperature: float = Field(default=0.7, description="模型生成温度")
    llm_timeout_seconds: int = Field(
        default=60,
        description="单次 LLM 调用超时(秒);避免模型卡住时整个搜索 Tool 阻塞",
    )


class ActionsSection(PluginConfigBase):
    """动作组件开关"""

    __ui_label__ = "动作"
    __ui_icon__ = "zap"
    __ui_order__ = 2

    image_search_enabled: bool = Field(
        default=False,
        description="是否启用图片搜索功能。支持 Bing、搜狗(国内可直连)和 DuckDuckGo(需翻墙)三个引擎自动降级。",
    )


class SearchBackendSection(PluginConfigBase):
    """搜索后端通用参数"""

    __ui_label__ = "搜索后端"
    __ui_icon__ = "globe"
    __ui_order__ = 3

    default_engine: Literal[
        "google", "bing", "sogou", "duckduckgo", "tavily", "anysearch", "exa", "you", "you_news", "deepseek"
    ] = Field(
        default="bing",
        description="默认搜索引擎",
    )
    max_results: int = Field(default=15, description="默认返回结果数量")
    timeout: int = Field(default=20, description="搜索超时时间(秒)")
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS 代理地址,例如 'http://127.0.0.1:7890'。留空表示不走代理。",
    )
    fetch_content: bool = Field(default=True, description="是否抓取网页内容")
    content_timeout: int = Field(default=10, description="内容抓取超时(秒)")
    max_content_length: int = Field(default=3000, description="最大内容长度")
    zhihu_cookies: str = Field(
        default="",
        description="知乎专用抓取使用的 Cookie 字符串;留空则不启用知乎专用抓取。",
    )
    user_agents: list[str] = Field(
        default_factory=lambda: [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        ],
        description="抓取网页时使用的 User-Agent 列表,会从中随机选择。",
    )


class EnginesSection(PluginConfigBase):
    """各搜索引擎的开关与专属参数"""

    __ui_label__ = "引擎"
    __ui_icon__ = "settings"
    __ui_order__ = 4

    # Google
    google_enabled: bool = Field(default=False, description="是否启用 Google 搜索")
    google_language: str = Field(default="zh-cn", description="搜索语言")

    # Bing
    bing_enabled: bool = Field(default=True, description="是否启用 Bing 搜索")
    bing_region: str = Field(default="zh-CN", description="Bing 搜索区域代码")

    # 搜狗
    sogou_enabled: bool = Field(default=True, description="是否启用搜狗搜索")

    # DuckDuckGo (DDGS)
    duckduckgo_enabled: bool = Field(default=True, description="是否启用 DDGS 元搜索引擎")
    duckduckgo_region: str = Field(default="wt-wt", description="搜索区域代码,例如 'us-en' 或 'cn-zh'")
    duckduckgo_backend: str = Field(
        default="auto",
        description="使用的后端。'auto' 表示自动选择,也可以指定多个,如 'duckduckgo,google,brave'",
    )
    duckduckgo_safesearch: Literal["on", "moderate", "off"] = Field(
        default="moderate",
        description="安全搜索级别",
    )
    duckduckgo_timelimit: Literal["none", "d", "w", "m", "y"] = Field(
        default="none",
        description="时间限制 (d, w, m, y;none 表示不限)",
    )

    # Tavily
    tavily_enabled: bool = Field(default=False, description="是否启用 Tavily 搜索")
    tavily_api_keys: list[str] = Field(
        default_factory=list,
        description="Tavily API key 列表，每项一把密钥；单个密钥也填写到列表中，多个密钥随机尝试",
    )
    tavily_search_depth: Literal["basic", "advanced"] = Field(default="basic", description="搜索深度")
    tavily_include_raw_content: bool = Field(default=False, description="是否返回网页原始内容")
    tavily_include_answer: bool = Field(default=True, description="是否返回 Tavily 生成的答案")
    tavily_topic: str = Field(
        default="",
        description=(
            "Tavily topic 参数(general/news);留空表示不传 topic,Tavily 走 general 模式。"
            "中文电竞/娱乐/社交场景**不建议**指定 news ——Tavily 的 news 索引偏向英文国际"
            "体育/政治资讯,中文检索准确率会显著下降。"
        ),
    )
    tavily_turbo: bool = Field(default=False, description="是否启用 Tavily Turbo 模式")

    # AnySearch
    anysearch_enabled: bool = Field(default=True, description="是否启用 AnySearch 搜索")
    anysearch_api_keys: list[str] = Field(
        default_factory=list,
        description="AnySearch API key 列表，每项一把密钥；多个密钥随机尝试，留空使用匿名请求",
    )
    anysearch_tag: str = Field(default="", description="搜索能力标签，例如 code.doc；留空由 AnySearch 自动选择")
    anysearch_zone: Literal["auto", "cn", "intl"] = Field(
        default="auto", description="搜索区域：auto 自动、cn 国内、intl 国际"
    )
    anysearch_language: str = Field(default="", description="搜索语言，例如 zh-CN 或 en-US；留空使用服务端默认值")
    anysearch_params: str = Field(
        default="",
        description='标签专属参数，填写 JSON 对象，例如 {"library":"golang"}；留空不传',
    )
    anysearch_format: Literal["json", "markdown"] = Field(default="json", description="API 返回内容格式")

    # Exa
    exa_enabled: bool = Field(default=False, description="是否启用 Exa 搜索")
    exa_api_keys: list[str] = Field(
        default_factory=list,
        description="Exa API key 列表，每项一把密钥；单个密钥也填写到列表中，多个密钥随机尝试",
    )
    exa_search_type: Literal["auto", "lite", "deep-lite", "deep"] = Field(
        default="auto",
        description="搜索类型：auto 自动、lite 低延迟缓存检索、deep-lite 轻量深度检索、deep 深度检索",
    )
    exa_category: str = Field(default="", description="内容类别，例如 news、research paper、company、people；留空不限")
    exa_include_domains: list[str] = Field(
        default_factory=list, max_length=1200, description="仅搜索这些域名或路径前缀，支持子域通配符；留空不限"
    )
    exa_exclude_domains: list[str] = Field(
        default_factory=list, max_length=1200, description="排除这些域名或路径前缀，最多 1200 项；people 类别不支持"
    )
    exa_start_published_date: str = Field(
        default="", description="最早发布日期，ISO 8601 格式；company/people 类别不支持"
    )
    exa_end_published_date: str = Field(default="", description="最晚发布日期，ISO 8601 格式；留空不限")
    exa_user_location: str = Field(default="", description="搜索位置的两字母国家代码，例如 CN 或 US；留空不指定")
    exa_moderation: bool = Field(default=False, description="是否过滤不适宜内容")
    exa_objective: str = Field(default="", max_length=4096, description="补充搜索目标，最多 4096 字符；留空不传")
    exa_additional_queries: list[str] = Field(
        default_factory=list, max_length=10, description="附加查询，最多 10 项；仅 deep-lite/deep 支持"
    )
    exa_include_highlights: bool = Field(default=True, description="返回与搜索相关的原文片段，不生成总结")
    exa_highlights_query: str = Field(default="", description="原文片段的提取目标；留空使用搜索问题")
    exa_highlights_max_characters: int = Field(
        default=0, ge=0, le=1000000, description="每条结果的片段字符上限，0 使用服务端默认值"
    )
    exa_include_text: bool = Field(default=False, description="在搜索响应中返回正文，不额外调用 Contents 接口")
    exa_text_max_characters: int = Field(
        default=0, ge=0, le=1000000, description="每条结果的正文字符上限，0 使用服务端默认值"
    )
    exa_max_age_hours: int = Field(
        default=-2, ge=-2, le=720, description="服务端缓存时效：-2 自动、-1 仅缓存、0 最新、正数为小时；lite 仅支持 -2"
    )
    exa_livecrawl_timeout_ms: int = Field(
        default=0, ge=0, le=90000, description="Exa 服务端获取内容的超时毫秒数；0 使用默认值，lite 仅支持 0"
    )

    @field_validator("anysearch_zone", mode="before")
    @classmethod
    def normalize_anysearch_zone(cls, value: object) -> object:
        # Legacy empty values mean automatic selection, not a blank select item.
        return "auto" if value == "" else value

    @field_validator("anysearch_params")
    @classmethod
    def validate_anysearch_params(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                params = json.loads(value)
            except ValueError:
                raise ValueError("AnySearch 标签参数必须是合法 JSON 对象") from None
            if not isinstance(params, dict):
                raise ValueError("AnySearch 标签参数必须是 JSON 对象")
        return value

    @field_validator("exa_include_domains", "exa_exclude_domains", "exa_additional_queries")
    @classmethod
    def clean_search_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("exa_user_location")
    @classmethod
    def validate_exa_location(cls, value: str) -> str:
        value = value.strip().upper()
        if value and (len(value) != 2 or not value.isascii() or not value.isalpha()):
            raise ValueError("Exa 搜索位置必须是两字母国家代码")
        return value

    @field_validator("exa_start_published_date", "exa_end_published_date")
    @classmethod
    def validate_exa_date(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Exa 发布日期必须是 ISO 8601 日期或时间") from None
        return value

    @model_validator(mode="after")
    def validate_exa_options(self) -> "EnginesSection":
        if self.exa_additional_queries and self.exa_search_type not in {"deep-lite", "deep"}:
            raise ValueError("Exa 附加查询仅支持 deep-lite/deep 模式")
        if self.exa_search_type == "lite" and (self.exa_max_age_hours != -2 or self.exa_livecrawl_timeout_ms != 0):
            raise ValueError("Exa lite 模式不支持自定义缓存时效或服务端抓取超时")
        category = self.exa_category.strip().lower()
        if category in {"company", "people"} and (self.exa_start_published_date or self.exa_end_published_date):
            raise ValueError("Exa company/people 类别不支持发布日期过滤")
        if category == "people" and self.exa_exclude_domains:
            raise ValueError("Exa people 类别不支持排除域名")
        return self

    # You
    you_enabled: bool = Field(default=False, description="是否启用 You Search")
    you_news_enabled: bool = Field(default=False, description="是否启用 You Live News(early access)")
    you_api_keys: list[str] = Field(
        default_factory=list,
        description="You API key 列表，每项一把密钥；Search、Live News、Contents 和 Images 共用",
    )
    you_freshness: str = Field(
        default="",
        description="You 搜索时间范围(可选:day/week/month/year;留空表示不限)",
    )
    you_offset: int = Field(default=0, description="You 搜索分页 offset(0-9,超出将被夹取)")
    you_country: str = Field(default="", description="You 搜索国家代码(如 CN/US)")
    you_language: str = Field(default="", description="You 搜索语言(BCP 47,如 EN/zh-Hans)")
    you_safesearch: str = Field(
        default="",
        description="You 安全搜索级别(off/moderate/strict;留空表示使用默认值)",
    )
    you_livecrawl: str = Field(
        default="",
        description="You Livecrawl 范围(web/news/all);留空表示不启用",
    )
    you_livecrawl_formats: str = Field(
        default="",
        description="You Livecrawl 内容格式(html/markdown);留空表示不启用",
    )
    you_contents_enabled: bool = Field(default=False, description="是否启用 You Contents 抓取")
    you_contents_format: Literal["html", "markdown"] = Field(
        default="markdown",
        description="You Contents 返回内容格式",
    )
    you_contents_force: bool = Field(
        default=False,
        description="是否强制使用 You Contents(不受搜索引擎来源限制)",
    )
    you_images_enabled: bool = Field(default=False, description="是否启用 You Images(early access)")

    # DeepSeek
    deepseek_enabled: bool = Field(default=False, description="是否启用 DeepSeek 官方联网搜索")
    deepseek_api_keys: list[str] = Field(
        default_factory=list,
        description="DeepSeek API key 列表，每项一把密钥；单个密钥也填写到列表中，多个密钥随机尝试",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek API 基础地址（自动适配 /anthropic/v1/messages 端点）",
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="DeepSeek 调用的模型名称（默认 deepseek-chat）",
    )
    deepseek_max_tokens: int = Field(
        default=2048,
        description="DeepSeek 生成答案的最大 Token 数",
    )


class TranslationSection(PluginConfigBase):
    """缩写翻译(神奇海螺 nbnhhsh)参数"""

    __ui_label__ = "缩写翻译"
    __ui_icon__ = "languages"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否启用缩写翻译工具")
    timeout_seconds: int = Field(default=10, description="API 请求超时(秒)")
    max_retries: int = Field(default=3, description="API 请求失败时的最大重试次数")


# ---------------------------------------------------------------------------
# Top-level plugin config
# ---------------------------------------------------------------------------


class GoogleSearchPluginConfig(PluginConfigBase):
    """google_search_plugin 顶层配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    actions: ActionsSection = Field(default_factory=ActionsSection)
    search_backend: SearchBackendSection = Field(default_factory=SearchBackendSection)
    engines: EnginesSection = Field(default_factory=EnginesSection)
    translation: TranslationSection = Field(default_factory=TranslationSection)
