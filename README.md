# Search 插件(qq:3103908461)

为麦麦提供网页搜索与答案总结，另支持图片搜索和网络缩写翻译。

支持 Google、Bing、搜狗、DuckDuckGo / DDGS、Tavily、AnySearch、Exa、You 和 DeepSeek；搜索失败时自动尝试其他已启用的引擎。

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
2. 使用 Tavily、AnySearch、Exa、You 或 DeepSeek 时，在对应的 API 密钥列表中添加密钥（AnySearch 可留空）。**只有一把密钥也填列表的一项**；多把密钥每项一把，会自动随机轮换。
3. 在“常用设置”选择默认引擎；使用 Google 等需要代理的网络环境时，按需填写代理地址。

选择默认引擎不会自动启用它，需先完成第 1 步。
总结直接复用麦麦的模型任务，通常无需额外配置模型接口。

Tavily、You、AnySearch 和 Exa 的密钥可分别前往 [Tavily](https://app.tavily.com)、[You](https://you.com/platform)、[AnySearch](https://anysearch.com) 和 [Exa](https://exa.ai) 官网获取。
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

### 缩写翻译

默认启用，基于神奇海螺 nbnhhsh 查询网络缩写和黑话，例如“yyds 是什么意思？”。
可在“常用设置”关闭。

## 查看运行状态

在群聊或私聊发送 `/google_search_status`，可查看当前默认引擎、启用引擎、模型任务和功能开关，排查配置是否生效。

## 鸣谢

[MaiBot](https://github.com/MaiM-with-u/MaiBot)

感谢 [heitiehu-beep](https://github.com/heitiehu-beep)、[wanshangovo](https://github.com/wanshangovo) 提供的代码优化与改进。

## Star History

[![Star History Chart](https://api.star-history.com/image?repos=XXXxx7258/google_search_plugin&type=timeline&legend=top-left)](https://www.star-history.com/?repos=XXXxx7258%2Fgoogle_search_plugin&type=timeline&legend=top-left)
