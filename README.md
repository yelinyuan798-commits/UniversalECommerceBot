# UniversalECommerceBot · 商家客服工作台

配置商品资料，在本机试聊，再连接商家消息页面。支持 FAQ、报价与议价下限、发货说明、未知问题转人工、消息防重和发送状态记录。

> **已接入 7 个平台：闲鱼、淘宝、京东已跑通真实店铺消息收发（作者实测确认，2026-11-03）；拼多多、抖店、快手小店、小红书千帆通过本地浏览器集成测试，待真实店铺验证。**
>
> 三平台实店结果由作者确认，本次维护未独立重做。浏览器接入需要登录自己的商家账号并校准页面选择器；本项目不直接控制手机原生 App。

## 能做什么

- **一键商品配置**：填写名称、平台商品 ID、价格、库存、发货和售后规则，生成可编辑配置；支持多商品、FAQ 和“不议价”。
- **本地试聊**：无需 AI 密钥即可测试商品问答；试聊与真实买家记录分开。
- **受约束 AI**：可选兼容 OpenAI 的模型用于意图/FAQ 选择；价格和商品承诺只取自已配置资料。模型失败退回规则，不发送模型自由编造的内容。
- **人工接管**：买家提出人工、投诉、退款，或知识缺失时进入待处理；接管后暂停自动回复，可在工作台恢复。
- **可靠收发**：按平台、店铺、会话及消息去重；会话和收件人分别校验。生成回复不等于发送成功；发送结果不明时不会自动重发。
- **观察模式**：只生成候选回复；自动回复需要明确启用。不会因买家说“已付款”而自动发货。

## 平台实现及验证边界

| 平台 | 接入方式 | 当前状态 |
| --- | --- | --- |
| [闲鱼](https://www.goofish.com/) | WebSocket、消息解析、心跳重连、发送回执 | **已跑通实店收发** · 作者实测确认 |
| [淘宝 / 千牛](https://myseller.taobao.com/) | 商家网页适配器、独立登录资料、会话与送达校验 | **已跑通实店收发** · 作者实测确认 |
| [京东 / 咚咚](https://dongdong.jd.com/) | 商家网页适配器、独立店铺与消息记录 | **已跑通实店收发** · 作者实测确认 |
| [拼多多](https://mms.pinduoduo.com/) | 商家网页适配器 | 本地集成测试通过，待实店验证 |
| [抖音 / 抖店](https://fxg.jinritemai.com/) | 商家网页适配器 | 本地集成测试通过，待实店验证 |
| [快手小店](https://login.kwaixiaodian.com/) | 商家网页适配器 | 本地集成测试通过，待实店验证 |
| [小红书 / 千帆](https://ark.xiaohongshu.com/) | 商家网页适配器 | 本地集成测试通过，待实店验证 |

实店状态确认日期为 **2026-11-03**，范围是作者所用店铺的消息收发。上述链接是平台官方入口，登录后仍需找到客服消息页。不同账号、页面版本和插件环境可能不同，仓库不包含作者的私有登录资料或店铺页面映射。

浏览器选择器默认留空，需要商家自行校准。**本地模拟页面的 `data-*` 属性不是各平台的通用页面选择器。** 目标页面需提供可靠的会话、消息、方向、创建时间及送达状态；无法提取时，应补充页面适配或使用获授权的官方接口。

淘宝与京东有官方客服相关接口，但涉及应用授权、请求上下文或插件环境。详细来源及逐项验收方法见 [平台接入说明](docs/PLATFORM_SETUP.md)。

## 安装与启动

需要 Python 3.11+。本机已安装的独立浏览器与聊天数据都在被忽略的 `data/` 下。

macOS / Linux：

```bash
git clone https://github.com/yelinyuan798-commits/UniversalECommerceBot.git
cd UniversalECommerceBot
bash setup.sh
bash start.command
```

Windows：双击 `setup.bat`，安装成功后双击 `start.bat`。

macOS 也可双击 `start.command`。若下载或网页上传后的文件没有执行权限，先运行 `chmod +x setup.sh start.command`；上面的 `bash` 启动方式不依赖执行权限。

也可以手动启动：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python main.py console
```

打开 <http://127.0.0.1:8765>。默认只监听本机，不是公网管理后台。

1. 在“商品与规则”填写资料，点击“一键生成并保存配置”。报价、禁止议价和转人工关键词使用明确字段控制；自由备注只辅助模型识别意图，不会自动变成任意业务操作。
2. 在右侧试聊价格、发货和转人工。多商品可通过“精细编辑”维护；商品可选择所属平台。同一商品 ID 在不同平台可以设置不同资料与价格；平台专用配置优先于“所有平台共用”。
3. 在“平台接入”启用目标平台并填写接入资料；先“开始观察”。缺失必需选择器或闲鱼登录信息会直接提示。
4. 用专用测试买家完成验收后，将目标平台 `dry_run` 设为 `false`，再“开启自动回复”。其他平台仍保持观察。观察期间见过的消息不会切换后补发；只处理后续新消息。
5. “消息与人工接管”查看回复与确认状态；售后事项由店主自行处理。

命令行模式：

```bash
.venv/bin/python main.py doctor       # 检查依赖与配置，不代表真实联调通过
.venv/bin/python main.py run          # 观察已启用平台
.venv/bin/python main.py run --live   # 仅 dry_run=false 的已启用平台允许发送
.venv/bin/python -m pytest -q         # 先安装 requirements-dev.txt
```

## 本地私有配置

- `config/merchant.json`：工作台生成的商品与规则。
- `config/platforms.local.json`：平台配置覆盖；未创建时使用 `config/platforms.json`。
- `config/.env`：可选 `API_KEY`、`MODEL_BASE_URL`、`MODEL_NAME`，以及闲鱼 `COOKIES_STR`。只在自己电脑编辑，勿贴到聊天、截图、Issue 或 Git 提交中。
- `data/`：浏览器登录资料、试聊/观察/真实服务 SQLite 记录和传输去重记录。不要公开上传。

无需 AI 密钥也能使用确定性规则。填写模型密钥后，商品资料和相应买家问题可能发送给该模型服务；是否使用由商家选择。`LLM_ENABLED=false` 可强制关闭模型调用。条件生成器是本地结构化解析，不声称理解并执行任意自然语言需求。

自动发货已从运行流程关闭，旧版 `config/delivery_items.json` 和旧 `core/agent.py`、`core/context.py` 不再由新入口使用；不能通过聊天文字判断订单已付款。没有修改真实订单、退款、改价或库存的能力。

## 测试

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
```

测试使用真实 Chromium 驱动**本地合成商家页面**，覆盖六个网页平台的读取与发送链路、商品规则和确认状态；同时验证闲鱼封包与错误路径、并发重复、会话隔离、最低价、不议价、模型失败与恶意输出、人工接管、配置校验及本机工作台访问边界。

CI 配置为在 Python 3.11 / 3.13 上运行同一套测试。完整结果与作者实店确认分开记录在 [验证记录](docs/TEST_REPORT.md)；本地模拟测试不替代真实店铺验收，受控模型响应不代表真实模型调用。

## 代码入口

```text
main.py                  # console / doctor / run
core/profiles.py         # 配置校验与条件生成
core/service.py          # 客服回复与人工接管
core/store.py            # SQLite 事件、上下文、状态
core/runtime.py          # 多平台启动与关闭
core/platform_catalog.py # 七平台目录、入口、别名与验证状态
platforms/browser.py     # 配置式网页接入
platforms/xianyu/        # 闲鱼协议实现
platforms/taobao/        # 淘宝网页适配器
platforms/jd/            # 京东网页适配器
platforms/pdd/           # 拼多多网页适配器
platforms/douyin/        # 抖店网页适配器
platforms/kuaishou/      # 快手小店网页适配器
platforms/xiaohongshu/   # 小红书千帆网页适配器
web/                     # 本机商品配置和试聊工作台
tests/                   # 单元与本地浏览器集成测试
```

当前默认采用“最多自动尝试一次发送”的取舍。超时、进程中断、页面变化可能留下待人工核实的消息；不会承诺永不漏单、永不掉线或无人管理。
