# Search 插件(qq:3103908461)

为麦麦提供网页搜索与答案总结，另支持图片搜索和网络缩写翻译。

支持 Google、Bing、搜狗、DuckDuckGo / DDGS、Tavily、Webz.io News Search、AnySearch、Exa、Keenable、You 和 DeepSeek；搜索失败时自动尝试其他已启用的引擎。

## 安装依赖

在**麦麦的 Python 运行环境**中，进入本插件目录后执行：

```bash
uv pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

未使用 uv 时，将命令中的 `uv pip` 改为 `pip` 即可。
一键包用户可在启动菜单的“交互式安装 pip 模块”中安装本插件的 `requirements.txt`。

## 快速上手

启动麦麦后，在 WebUI 中打开本插件配置。**默认使用 Bing，无需 API 密钥**；直接在对话中提出需要联网的问题即可。

想更换搜索引擎时：

1. 在“搜索引擎”页启用需要的引擎。
2. 需要密钥的引擎每项填一把，多把密钥会随机轮换。AnySearch 和 Keenable 可留空。
3. 在“常用设置”选择默认引擎；使用 Google 等需要代理的网络环境时，按需填写代理地址。

选择默认引擎不会自动启用它，需先完成第 1 步。
总结直接复用麦麦的模型任务，通常无需额外配置模型接口。

密钥申请：[Tavily](https://app.tavily.com)、[Webz.io](https://webz.io)、[You](https://you.com/platform)、[AnySearch](https://anysearch.com)、[Exa](https://exa.ai)、[Keenable](https://app.keenable.ai)。

Webz.io News Search 默认关闭。启用后检索近 30 天的全球新闻。密钥填在搜索引擎页；留空时使用 `WEBZ_API_TOKEN`。

Keenable 默认关闭。启用后直接返回完整片段，条数可在「常用设置」调整。无论是否填写密钥，服务商[默认记录搜索词、结果和 IP 地址](https://docs.keenable.ai/data-privacy)。

作者提供的临时邮箱：[邮箱 1](https://xiaowan258.me)、[邮箱 2](https://mail.xiaowan.me)。

## 配置入口

| WebUI 标签页 | 用途 |
| --- | --- |
| 常用设置 | 默认引擎、结果数量、代理、总结模型和功能开关 |
| 搜索引擎 | 各引擎的开关、API 密钥列表和基本参数 |
| 高级设置 | 超时、正文抓取和引擎高级参数，默认折叠 |

具体选项查看字段旁的提示即可，不确定时保持默认。
如需手动编辑，配置文件位于 `plugins/google_search_plugin/config.toml`，首次启动后生成。

## 在对话中使用

直接向麦麦提问，例如“能搜一下《Ave Mujica》吗？”，麦麦会在需要联网时搜索并总结结果。

### 图片搜索

在“常用设置”开启图片搜索后，可以提出“找一张千早爱音的图片”之类的请求。
支持 Bing、搜狗、DuckDuckGo 和 You Images，不受默认网页搜索引擎影响；You Images 需账号开通 early access 权限。

<img src="0d116086-0df6-4694-97d3-28d521184223.png" alt="千早爱音示例" width="400">

### 以图搜图

发一张图片并问麦麦“这张图出自哪里？”，也可以提供一个公网图片链接。识图会把图片或链接交给启用的搜索服务，不适合发送私人图片。聊天图片优先用 Google Lens；它不可用时会尝试 Yandex。必应需要图片链接，只有聊天图片时依赖 Yandex 提供上传后的地址。如需通过代理下载图片，请在插件设置中填写代理地址。

Google Lens 页面上如果有 AI 概览，插件会优先返回它，不必等关联网页出现。如果网页先出现，默认最多再等 6 秒；仍未读到概览时，会明确说明并返回链接，不会用麦麦生成的摘要代替。这样的纯链接结果最多缓存 15 秒，之后再搜会重新查询。等待时间可在「高级设置 → 以图搜图引擎参数」中调整，设为 0 表示不额外等待。

Google Lens 首次使用时需要启动浏览器。要区分启动慢、搜索慢还是重复图片直接命中缓存，可以先看麦麦的工具调用耗时，再对照插件日志中的浏览器启动、上传、导航、结果提取和各引擎耗时。也可以在麦麦项目根目录运行以下命令；它使用公开的 Python 标志和独立的临时浏览器目录，不读取账号登录资料：

```bash
uv run --no-sync python plugins/google_search_plugin/tools/probe_reverse_image.py --config plugins/google_search_plugin/config.toml
```

输出依次为首次调用、清除搜索结果缓存后的重复搜索、保留缓存的重复调用。判断热搜耗时时要看实际返回结果的引擎；如果 Google 已进入冷却，后续测到的是备用引擎。

### 缩写翻译

默认启用，基于神奇海螺 nbnhhsh 查询网络缩写和黑话，例如“yyds 是什么意思？”。
可在“常用设置”关闭。

## 查看运行状态

在群聊或私聊发送 `/google_search_status`，可查看当前默认引擎、启用引擎、模型任务和功能开关，排查配置是否生效。

## 鸣谢

[MaiBot](https://github.com/MaiM-with-u/MaiBot)

感谢 [heitiehu-beep](https://github.com/heitiehu-beep)、[wanshangovo](https://github.com/wanshangovo)、[KGKallasmaa](https://github.com/KGKallasmaa) 对插件的贡献。

## Star History

[![Star History Chart](https://api.star-history.com/image?repos=XXXxx7258/google_search_plugin&type=timeline&legend=top-left)](https://www.star-history.com/?repos=XXXxx7258%2Fgoogle_search_plugin&type=timeline&legend=top-left)
