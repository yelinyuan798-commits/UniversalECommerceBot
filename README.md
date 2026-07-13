
<div align="center">

# 🌐 通用电商 AI 自动客服系统

### 🚀 一套代码跑通10+平台 · 24小时无人值守 · AI自动售卖

[![Download ZIP](https://img.shields.io/badge/⬇️_Download_ZIP-181717?style=for-the-badge&logo=github)](https://github.com/yelinyuan798-commits/UniversalECommerceBot/archive/refs/heads/main.zip)
[![Quick Deploy](https://img.shields.io/badge/🚀_3分钟部署-22AA55?style=for-the-badge)](https://github.com/yelinyuan798-commits/UniversalECommerceBot#%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B)
[![View on GitHub](https://img.shields.io/badge/📖_查看文档-4A90D9?style=for-the-badge)](https://github.com/yelinyuan798-commits/UniversalECommerceBot)
[![Star](https://img.shields.io/badge/⭐_Star-FF6B6B?style=for-the-badge)](https://github.com/yelinyuan798-commits/UniversalECommerceBot)

</div>

---

## ⬇️ 下载与部署

```bash
# 方式一：下载ZIP（推荐）
# 点击上方 "Download ZIP" 按钮 → 解压 → 双击 setup.bat

# 方式二：克隆
git clone https://github.com/yelinyuan798-commits/UniversalECommerceBot.git
cd UniversalECommerceBot

# 一键部署
双击 setup.bat    # ← 自动完成所有安装
# 或命令行运行 setup.bat
```

---
## 🎯 这是什么神仙工具？

**一个 AI，接管你所有店铺的客服。**

你只需要把项目复制到电脑上 → 填上各个平台的 Cookie → 启动。然后：

- 🤖 **买家发消息** → AI 自动回复（比人工快、比人工专业）
- 💰 **买家砍价** → AI 自动议价（阶梯让步，利润最大化）
- 📦 **买家付款** → AI 自动发货（网盘链接秒发）
- 🔄 **断线了** → AI 自动重连（7×24 永不掉线）
- 🚀 **重启电脑** → AI 自动启动（开机自启，真正的无人值守）

> **你唯一需要做的事：数钱。**

---

## 🌍 支持 10+ 电商平台

| 平台 | 状态 | 说明 |
|------|------|------|
| 🟢 **闲鱼** | ✅ **已跑通** | WebSocket 实时连接，自动回复+发货 |
| 🔧 淘宝/天猫 | 📋 模板就绪 | API 对接中 |
| 🔧 拼多多 | 📋 模板就绪 | API 对接中 |
| 🔧 京东 | 📋 模板就绪 | API 对接中 |
| 🔧 抖音电商 | 📋 模板就绪 | API 对接中 |
| 🔧 快手电商 | 📋 模板就绪 | API 对接中 |
| 🔧 小红书 | 📋 模板就绪 | API 对接中 |
| 🔧 Amazon | 📋 模板就绪 | API 对接中 |
| 🔧 Shopify | 📋 模板就绪 | API 对接中 |
| 🔧 微店 | 📋 模板就绪 | API 对接中 |

> **接入新平台只需实现 6 个接口方法**，核心 AI 引擎、数据库、发货系统全部复用。

---

## ✨ 核心能力

### 🤖 AI 智能客服（比真人更靠谱）
```
买家："老板这个还有吗"
  → AI识别意图 → 生成回复 → 秒回
  → "有的亲，全新正品，拍下马上发货~"

买家："能便宜点吗"
  → AI记录议价次数 → 阶梯让步策略
  → "亲，最低给您便宜10块，再送个小礼品~"

买家："支持XX功能吗"
  → AI技术专家模式 → 参数解读
  → "支持的，这款采用最新技术..."
```

### 📦 自动发货（付款即发，永不漏单）
```
检测到"等待卖家发货"
  → 匹配商品ID → 读取配置 → 发送网盘链接+提取码
  → 记录防重复 → 提醒补上架
```

### 🔄 7×24 永不掉线
```
崩溃 → 自动重启 → 重新连接 → 继续值守
                    ↕
            Token 自动刷新
                    ↕
            心跳实时维持
```

---

## 🚀 3 步开始

```bash
# 第 1 步：双击 setup.bat（自动装好所有依赖）

# 第 2 步：编辑 config\.env 填参数
#   API_KEY=你的DeepSeek密钥
#   COOKIES_STR=闲鱼Cookie

# 第 3 步：双击 start.bat → AI 开始自动值守！
```

> **全程不需要写代码，不需要懂技术。**  
> **只需要填 2 个参数：API Key + Cookie。**

---

## 🏛️ 技术架构（简洁版）

```
          ┌──────────────────────────────────────────┐
          │              🌐 main.py                   │
          │         多平台启动器 · 统一消息路由        │
          └──────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │   闲鱼     │   │   淘宝     │   │   拼多多   │
   │ WebSocket  │   │   API      │   │   API      │
   └──────┬─────┘   └──────┬─────┘   └──────┬─────┘
          │                │                │
          └────────────────┼────────────────┘
                           ▼
          ┌──────────────────────────────────────────┐
          │           🧠 核心 AI 引擎                │
          │   LLM 意图识别 → 路由分发 → 回复生成    │
          ├──────────────────────────────────────────┤
          │   📦 自动发货 · 💰 智能议价              │
          │   🗄️ 数据库 · 🔄 心跳维持                │
          └──────────────────────────────────────────┘
```

---

## 📦 项目结构（极简版）

```
UniversalECommerceBot/
├── main.py                 # 🎯 一键启动所有平台
├── core/                   # 🧠 AI 核心引擎
│   ├── base_platform.py    #    平台接口（6个方法）
│   ├── agent.py            #    LLM 智能回复
│   └── context.py          #    数据库管理
├── platforms/              # 🔌 各平台实现
│   ├── xianyu/             #    ✅ 闲鱼（已跑通）
│   └── 更多平台待接入...    #    📋 模板就绪
├── config/                 # ⚙️ 配置
│   ├── .env.example
│   ├── platforms.json
│   └── delivery_items.json
├── setup.bat               # ⚡ 双击装好一切
└── start.bat               # ▶️ 双击开始赚钱
```

---

## 🔌 接入新平台（开发者）

实现 6 个方法，你的平台就活了：

```python
from core.base_platform import BasePlatform

class MyPlatformBot(BasePlatform):
    async def connect(self): ...        # 连接平台
    async def disconnect(self): ...     # 断开连接
    async def start(self, handler): ... # 开始监听
    async def send_message(self, user_id, msg): ...    # 发消息
    async def send_delivery(self, user_id, item_id, info): ... # 发货
    async def get_item_info(self, item_id): ...        # 查商品
```

注册到 `config/platforms.json` 即可。

---

## 📄 开源协议

MIT License

---

<div align="center">

### ⭐ 如果这个项目帮到了你，点个 Star 支持一下！

**你的 Star 是作者持续更新的动力 ❤️**

[![GitHub stars](https://img.shields.io/github/stars/yelinyuan798-commits/UniversalECommerceBot?style=social)](https://github.com/yelinyuan798-commits/UniversalECommerceBot)

</div>

