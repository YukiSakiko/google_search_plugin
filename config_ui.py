"""WebUI 展示布局；虚拟分块的 name 始终指向原有 TOML 配置节。"""

from dataclasses import dataclass
from typing import Any

from .config import ENGINE_LABELS


@dataclass(frozen=True)
class _Section:
    source: str
    title: str
    description: str
    fields: dict[str, str]


_SECTIONS = {
    "search": _Section(
        "search_backend",
        "搜索设置",
        "每行选一个搜索引擎，排在最上面的优先使用。",
        {
            "engine_order": "搜索引擎顺序",
            "max_results": "搜索结果数量",
            "fetch_content": "额外抓取网页正文",
            "proxy": "代理地址",
        },
    ),
    "model": _Section(
        "models",
        "总结模型",
        "复用麦麦已有的模型任务，无需在这里重复配置模型接口。",
        {"model_name": "总结使用的模型任务"},
    ),
    "images": _Section(
        "actions",
        "图片搜索",
        "按需开启；候选图片由麦麦决定是否发送，不受网页搜索顺序影响。",
        {"image_search_enabled": "启用图片搜索"},
    ),
    "reverse_image": _Section(
        "reverse_image",
        "以图搜图（识图）",
        "识别聊天中的图片出处、角色或梗图；默认优先 Google Lens，遇风控自动降级到 Yandex 和 Bing。",
        {"enabled": "启用以图搜图"},
    ),
    "translation": _Section(
        "translation",
        "缩写翻译",
        "通过神奇海螺 nbnhhsh 查询网络缩写和黑话。",
        {"enabled": "启用缩写翻译"},
    ),
    "plugin": _Section("plugin", "插件开关", "控制整个插件是否启用。", {"enabled": "启用插件"}),
    "google": _Section(
        "engines",
        "Google",
        "通用网页与跨语言资料检索；需可访问 Google 的网络环境。",
        {"google_enabled": "启用 Google", "google_language": "搜索语言"},
    ),
    "bing": _Section(
        "engines",
        "Bing",
        "通用网页与图片检索，适合日常资料查询；需可访问 Bing 的网络环境。",
        {"bing_enabled": "启用 Bing", "bing_region": "搜索区域"},
    ),
    "sogou": _Section(
        "engines",
        "搜狗",
        "偏中文网页与图片检索；需可访问搜狗的网络环境。",
        {"sogou_enabled": "启用搜狗"},
    ),
    "duckduckgo": _Section(
        "engines",
        "DuckDuckGo / DDGS",
        "聚合多个搜索后端，适合补充检索；网络需可访问所选后端。",
        {"duckduckgo_enabled": "启用 DDGS"},
    ),
    "tavily": _Section(
        "engines",
        "Tavily",
        "面向 AI 应用提供检索资料与答案；需可访问 Tavily API 的网络环境。",
        {"tavily_enabled": "启用 Tavily", "tavily_api_keys": "API 密钥列表"},
    ),
    "webz": _Section(
        "engines",
        "Webz.io News Search",
        "检索近 30 天的全球新闻；需可访问 Webz.io API 的网络环境。",
        {"webz_enabled": "启用 Webz.io News Search", "webz_api_keys": "API 密钥列表"},
    ),
    "anysearch": _Section(
        "engines",
        "AnySearch",
        "通用网页检索与正文片段获取；需可访问 AnySearch API 的网络环境。",
        {"anysearch_enabled": "启用 AnySearch", "anysearch_api_keys": "API 密钥列表"},
    ),
    "exa": _Section(
        "engines",
        "Exa",
        "语义检索与研究资料查找；需可访问 Exa API 的网络环境。",
        {"exa_enabled": "启用 Exa", "exa_api_keys": "API 密钥列表"},
    ),
    "keenable": _Section(
        "engines",
        "Keenable",
        "网页检索，可不填密钥使用匿名接口；需可访问 Keenable API 的网络环境。",
        {"keenable_enabled": "启用 Keenable", "keenable_api_keys": "API 密钥列表"},
    ),
    "you": _Section(
        "engines",
        "You Search",
        "面向 AI 应用检索网页与新闻资料；需可访问 You API 的网络环境。",
        {"you_enabled": "启用 You Search", "you_api_keys": "API 密钥列表"},
    ),
    "deepseek": _Section(
        "engines",
        "DeepSeek 联网搜索",
        "结合联网资料生成回答；需可访问 DeepSeek API 的网络环境。",
        {"deepseek_enabled": "启用 DeepSeek", "deepseek_api_keys": "API 密钥列表"},
    ),
    "fetch_advanced": _Section(
        "search_backend",
        "请求与网页抓取",
        "调整超时、单页正文长度和特殊站点抓取；通常无需修改。",
        {
            "timeout": "搜索请求超时（秒）",
            "content_timeout": "正文抓取超时（秒）",
            "max_content_length": "单页正文上限（字符）",
            "zhihu_cookies": "知乎 Cookie",
            "user_agents": "User-Agent 列表",
        },
    ),
    "model_advanced": _Section(
        "models",
        "总结模型参数",
        "仅影响插件通过宿主模型进行的本地总结。",
        {"temperature": "生成温度", "llm_timeout_seconds": "单次模型调用超时（秒）"},
    ),
    "duckduckgo_advanced": _Section(
        "engines",
        "DDGS 高级参数",
        "自定义元搜索后端、区域和结果过滤。",
        {
            "duckduckgo_region": "搜索区域",
            "duckduckgo_backend": "元搜索后端",
            "duckduckgo_safesearch": "安全搜索级别",
            "duckduckgo_timelimit": "搜索时间范围",
        },
    ),
    "tavily_advanced": _Section(
        "engines",
        "Tavily 高级参数",
        "中文日常搜索通常保持默认，主题留空即可。",
        {
            "tavily_search_depth": "搜索深度",
            "tavily_include_answer": "返回引擎生成的答案",
            "tavily_include_raw_content": "返回网页原始内容",
            "tavily_topic": "搜索主题",
            "tavily_turbo": "启用 Turbo 模式",
        },
    ),
    "webz_advanced": _Section(
        "engines",
        "Webz.io News Search 参数",
        "按需限制发布时间、语言、国家、情感、来源域名和股票代码。",
        {
            "webz_days": "最近天数",
            "webz_language": "文章语言",
            "webz_country": "来源国家",
            "webz_sentiment": "情感",
            "webz_domains": "来源域名",
            "webz_tickers": "股票代码",
        },
    ),
    "anysearch_advanced": _Section(
        "engines",
        "AnySearch 搜索参数",
        "按需指定搜索类别、区域、语言和底层引擎参数。",
        {
            "anysearch_tag": "搜索类别标签",
            "anysearch_zone": "搜索区域",
            "anysearch_language": "搜索语言",
            "anysearch_params": "底层引擎参数（JSON）",
            "anysearch_format": "返回内容格式",
        },
    ),
    "keenable_advanced": _Section(
        "engines",
        "Keenable 搜索参数",
        "按需限制网页发布时间。",
        {"keenable_published_after": "最早发布时间"},
    ),
    "you_advanced": _Section(
        "engines",
        "You 搜索参数",
        "按需限制搜索范围；Live News 需要账号开通 early access 权限。",
        {
            "you_news_enabled": "启用 You Live News",
            "you_freshness": "搜索时间范围",
            "you_offset": "搜索分页偏移",
            "you_country": "搜索国家",
            "you_language": "搜索语言",
            "you_safesearch": "安全搜索级别",
            "you_livecrawl": "实时抓取范围",
            "you_livecrawl_formats": "实时抓取格式",
        },
    ),
    "exa_advanced": _Section(
        "engines",
        "Exa 搜索参数",
        "按需筛选来源、发布时间和内容类别，或调整研究查询。",
        {
            "exa_search_type": "搜索类型",
            "exa_category": "内容类别",
            "exa_include_domains": "限定搜索域名",
            "exa_exclude_domains": "排除搜索域名",
            "exa_start_published_date": "最早发布时间",
            "exa_end_published_date": "最晚发布时间",
            "exa_user_location": "用户位置",
            "exa_moderation": "内容审核",
            "exa_objective": "研究目标",
            "exa_additional_queries": "补充检索查询",
        },
    ),
    "exa_content": _Section(
        "engines",
        "Exa 内容参数",
        "调整返回片段、正文长度和服务端内容新鲜度；lite 不支持自定义抓取参数。",
        {
            "exa_include_highlights": "返回相关片段",
            "exa_highlights_query": "片段提取查询",
            "exa_highlights_max_characters": "片段字符上限",
            "exa_include_text": "返回网页正文",
            "exa_text_max_characters": "正文字符上限",
            "exa_max_age_hours": "允许的缓存时长（小时）",
            "exa_livecrawl_timeout_ms": "服务端实时抓取超时（毫秒）",
        },
    ),
    "you_extensions": _Section(
        "engines",
        "You 正文与图片扩展",
        "可选的 Contents 和 Images 服务，后者需要 early access 权限。",
        {
            "you_contents_enabled": "启用 You Contents 正文抓取",
            "you_contents_format": "Contents 正文格式",
            "you_contents_force": "对其他引擎结果使用 Contents",
            "you_images_enabled": "启用 You Images",
        },
    ),
    "deepseek_advanced": _Section(
        "engines",
        "DeepSeek 接口参数",
        "仅在需要修改接口地址、模型或答案长度时调整。",
        {
            "deepseek_base_url": "API 基础地址",
            "deepseek_model": "联网搜索模型",
            "deepseek_max_tokens": "答案最大 Token 数",
        },
    ),
    "reverse_image_advanced": _Section(
        "reverse_image",
        "以图搜图引擎参数",
        "配置 Google Lens 持久化目录或 Cookie、备用引擎与正文补充策略。",
        {
            "google_lens_enabled": "启用 Google Lens",
            "google_lens_cookies": "Google Lens 登录 Cookie",
            "google_lens_profile_dir": "Google Lens 持久化目录",
            "google_lens_headless": "Google Lens 无头模式",
            "google_lens_browser_channel": "Google Lens 浏览器内核",
            "google_lens_overview_wait_seconds": "等待 Google AI 概览（秒）",
            "yandex_enabled": "启用 Yandex 识图",
            "bing_enabled": "启用 Bing 视觉搜索",
            "max_results": "以图搜图结果数量",
            "fetch_content": "额外抓取关联网页正文",
            "cache_ttl_seconds": "同图缓存时长（秒）",
        },
    ),
    "translation_advanced": _Section(
        "translation",
        "缩写翻译请求参数",
        "仅影响神奇海螺 nbnhhsh 请求，不影响网页搜索。",
        {"timeout_seconds": "请求超时（秒）", "max_retries": "最大重试次数"},
    ),
    "plugin_info": _Section(
        "plugin",
        "插件信息",
        "用于版本识别，保持现有值；仅在配置迁移或插件发布时调整。",
        {"name": "插件名称", "version": "插件版本", "config_version": "配置版本"},
    ),
}

_TABS = {
    "basic": ("常用设置", ("search", "model", "images", "reverse_image", "translation", "plugin")),
    "engines": (
        "搜索引擎",
        ("google", "bing", "sogou", "duckduckgo", "tavily", "webz", "anysearch", "exa", "keenable", "you", "deepseek"),
    ),
    "advanced": (
        "高级设置",
        (
            "fetch_advanced",
            "model_advanced",
            "reverse_image_advanced",
            "duckduckgo_advanced",
            "tavily_advanced",
            "anysearch_advanced",
            "exa_advanced",
            "exa_content",
            "keenable_advanced",
            "webz_advanced",
            "you_advanced",
            "you_extensions",
            "deepseek_advanced",
            "translation_advanced",
            "plugin_info",
        ),
    ),
}

_HINTS = {
    "search_backend.engine_order": (
        "从上到下依次尝试，拖动每行左侧的手柄可调整顺序。请求失败或没有结果时尝试下一个，找到结果就停止。"
        "只用列表中已启用且满足密钥要求的引擎；移除后不再尝试，清空后不进行网页搜索。"
    ),
    "search_backend.max_results": (
        "每次网页搜索期望返回的条数。Keenable 最多返回 50 条，Webz.io 最多返回 100 条，实际结果可能更少。"
        "这两个引擎都会保留完整片段；想减少返回内容，可调低条数。"
    ),
    "search_backend.fetch_content": "搜索后额外抓取网页正文。关闭可减少请求和延迟，不影响引擎自带的内容或答案。",
    "search_backend.proxy": "搜索请求和普通网页抓取使用的显式 HTTP/HTTPS 代理；留空不指定代理，部分请求仍可能使用环境代理。并非所有引擎或图片下载都使用此设置。",
    "search_backend.timeout": "单个搜索请求的超时秒数；多密钥尝试或引擎降级时耗时可能累加，并非整个搜索流程的总超时。",
    "search_backend.max_content_length": "单页抓取正文及 AnySearch/Exa 直返片段的最大字符数，不是总长度或 Token 数；不限制其他引擎内联内容或 DeepSeek 直接生成的答案。",
    "models.model_name": "搜索结果总结使用的宿主模型任务，通常保持 replyer；具体模型由麦麦对应任务的配置决定。",
    "models.temperature": "本地搜索总结的生成温度；不影响 DeepSeek 服务端直接生成的答案。",
    "models.llm_timeout_seconds": "单次本地 LLM 总结调用的超时秒数，并非整个搜索流程的总超时。",
    "engines.keenable_enabled": (
        "启用并加入搜索顺序后，轮到 Keenable 时就会向它发送搜索内容。"
        "无论是否填写密钥，服务商默认都会记录搜索内容、搜索结果和 IP 地址。"
    ),
    "engines.you_contents_force": "允许对其他引擎的结果使用 You Contents；仍需开启网页正文抓取和 You Contents，并提供有效密钥。",
    "engines.you_images_enabled": "需账号开通 early access 权限，并在常用设置开启图片搜索；与 You Search、Contents 共用搜索引擎页的 You 密钥列表。",
    "engines.anysearch_params": "可选的 JSON 对象字符串，仅填写对应底层引擎支持的参数；留空不额外传参。",
    "engines.anysearch_zone": "auto 自动选择搜索区域，cn 使用国内区域，intl 使用国际区域；自动模式不向 API 传入区域限制。",
    "engines.exa_search_type": "auto 自动选择，lite 偏向低延迟，deep-lite 和 deep 用于研究型检索；lite 不支持补充查询或自定义缓存时长、实时抓取超时。",
    "engines.exa_category": "可选的内容分类；company/people 不支持发布时间过滤，people 也不支持排除域名。",
    "engines.exa_include_domains": "仅检索这些域名或 URL 前缀，每项一条；留空不限定来源。",
    "engines.exa_exclude_domains": "排除这些域名或 URL 前缀，每项一条；people 分类不支持此选项。",
    "engines.exa_start_published_date": "可选的 ISO 8601 时间，用于限定最早发布时间；company/people 分类不支持。",
    "engines.exa_end_published_date": "可选的 ISO 8601 时间，用于限定最晚发布时间；company/people 分类不支持。",
    "engines.exa_additional_queries": "补充检索查询，每项一条；lite 不支持，留空不传。",
    "engines.exa_highlights_query": "用于提取相关片段的查询；留空使用搜索查询。",
    "engines.exa_highlights_max_characters": "每条结果的相关片段字符上限；0 使用服务端默认值。",
    "engines.exa_text_max_characters": "每条结果的网页正文字符上限；0 使用服务端默认值。",
    "engines.exa_max_age_hours": "-2 由服务端自动决定，-1 仅使用缓存，0 请求最新内容，1-720 为允许的缓存小时数；lite 只能使用 -2。",
    "engines.exa_livecrawl_timeout_ms": "服务端实时抓取超时，单位为毫秒；0 使用默认值，1-90000 指定超时；lite 只能使用 0。",
    "engines.webz_days": "0 表示只看当天，30 表示最近 30 天；按发布日期过滤。",
    "engines.webz_country": "来源国家的两字母代码，例如 US；留空不限。",
    "engines.webz_sentiment": "auto 不限情感，也可以选择 positive、negative 或 neutral。",
    "engines.webz_domains": "只保留这些来源域名，每项一条；留空不限。",
    "engines.webz_tickers": "相关公司的股票代码，每项一条，例如 NVDA；留空不限。",
    "reverse_image.enabled": "开启后向麦麦暴露 reverse_image_search 工具，用于识别群聊或私聊图片出处、角色与梗图。",
    "reverse_image.google_lens_enabled": "优先使用 Google Lens（准确率最高）；需要可访问 Google 的代理及本机浏览器/Playwright，遇风控会自动降级。",
    "reverse_image.google_lens_cookies": "适合无桌面环境的 Docker/Linux 服务器部署：粘贴 google.com 登录态 Cookie 字符串；留空则使用本地持久化浏览器目录。",
    "reverse_image.google_lens_profile_dir": "本地持久化浏览器用户数据目录；桌面端可直接在本页点击「打开浏览器登录 Google」按钮完成登录并长期复用。留空且未填 Cookie 时默认存于插件目录下 .google_lens_profile。",
    "reverse_image.google_lens_headless": "开启后在后台无界面运行浏览器；本地排障或需要观察页面时可临时关闭。",
    "reverse_image.google_lens_browser_channel": "auto 优先尝试本机 Chrome/Edge 并回退 Playwright 内置 Chromium；Docker 环境可选 chromium。",
    "reverse_image.google_lens_overview_wait_seconds": "如果关联网页先出现，默认最多再等 6 秒。概览一出现就返回；设为 0 则不额外等待。",
    "reverse_image.yandex_enabled": "纯 HTTP 识图引擎，免 Cookie、免浏览器，二次元角色与原图识别表现好。",
    "reverse_image.bing_enabled": "必应支持直接搜索图片链接；聊天图片需要先由 Yandex 提供图片链接，才能用必应补充结果。",
    "reverse_image.max_results": "每次以图搜图返回给麦麦的最大关联页面条数。",
    "reverse_image.fetch_content": "开启后会额外抓取前 2 个关联页面的正文片段；默认关闭以加快响应，麦麦可按需再用 web_search 访问具体链接。",
    "reverse_image.cache_ttl_seconds": "相同图片的缓存有效秒数，避免群聊反复发送同一张表情包时重复请求；0 表示关闭缓存。",
}


def build_config_ui_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """重组 SDK Schema，不修改默认值、字段类型或配置读写路径。"""
    sections: dict[str, Any] = {}
    tabs: list[dict[str, Any]] = []
    for tab_id, (title, section_ids) in _TABS.items():
        tabs.append({"id": tab_id, "title": title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _SECTIONS[section_id]
            source = schema["sections"][spec.source]
            fields: dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                field = dict(source["fields"][field_name])
                path = f"{spec.source}.{field_name}"
                field.update(
                    label=label,
                    hint=_HINTS.get(path, field["description"]),
                    order=order,
                )
                if path == "search_backend.engine_order":
                    field["item_fields"] = {
                        "engine": {
                            **field["item_fields"]["engine"],
                            "type": "select",
                            "choices": list(ENGINE_LABELS.values()),
                            "placeholder": "请选择引擎",
                        }
                    }
                if path in {"search_backend.zhihu_cookies", "reverse_image.google_lens_cookies"}:
                    field["ui_type"] = "password"
                if spec.source == "engines" and field_name.endswith("_api_keys"):
                    field["hint"] = (
                        "每项填写一把 API 密钥，只有一把时添加一项即可；多项自动轮换。"
                        "列表会明文显示密钥，分享截图或配置前请隐藏。"
                    )
                    field["placeholder"] = "添加一把 API 密钥"
                    if field_name == "anysearch_api_keys":
                        field["hint"] = "留空可使用 AnySearch 匿名搜索，额度由服务端限制。" + field["hint"]
                    if field_name == "keenable_api_keys":
                        field["hint"] = "留空可使用 Keenable 匿名接口，按 IP 限流。" + field["hint"]
                    if field_name == "webz_api_keys":
                        field["hint"] = "留空时使用 WEBZ_API_TOKEN。" + field["hint"]
                if section_id == "plugin_info":
                    field["disabled"] = True
                fields[field_name] = field
            # 字典键只标识展示分块，name 才是 WebUI 读取和保存的真实配置节。
            sections[section_id] = {
                **source,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": tab_id == "advanced",
                "order": len(sections),
                "fields": fields,
            }
    return {**schema, "sections": sections, "layout": {"type": "tabs", "tabs": tabs}}
