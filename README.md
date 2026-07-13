<div align="center">

# 🌐 Universal E-Commerce Bot
# 通用电商 AI 自动客服系统

### 🚀 闲鱼 / 淘宝 / 拼多多 — 一套系统，全平台覆盖

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)]()
[![License](https://img.shields.io/badge/License-MIT-22AA55?style=flat-square)]()
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-4A90D9?style=flat-square)]()
[![Platform](https://img.shields.io/badge/Platforms-Xianyu%20%7C%20Taobao%20%7C%20PDD-FF6B6B?style=flat-square)]()

</div>

---

## 📦 一套系统，跑通所有平台

**目前已实现：** ✅ **闲鱼**（已跑通 WebSocket 连接 + AI 自动回复 + 自动发货）
**模板就绪：** 🔧 **淘宝 / 拼多多**（只需实现平台接口即可接入）

```
复制项目 → 双击 setup.bat → 填 Cookie → 启动
        ↓
   ┌─────────────────┐
   │    AI 核心引擎    │ ← LLM 多 Agent 系统
   ├─────────────────┤
   │  闲鱼 │ 淘宝 │ 拼多多 │ ← 各自实现平台接口
   ├─────────────────┤
   │   数据库 │ 发货 │ 日志 │ ← 共享服务
   └─────────────────┘
```

---

## ✨ 核心特性

| 功能 | 说明 |
|------|------|
| 🌍 **多平台支持** | 一套核心引擎，适配所有电商平台 |
| 🧠 **AI 智能回复** | LLM 自动识别买家意图，生成专业回复 |
| 💰 **智能议价** | 阶梯让步策略，既保利润又提成交率 |
| 📦 **自动发货** | 付款即发，百度网盘链接自动推送 |
| 🔌 **极简接入** | 实现 5 个接口即可接入新平台 |
| ⚡ **一键部署** | `setup.bat` 自动完成全部安装 |
| 🔄 **永不掉线** | 崩溃重启 + Token 刷新 + 心跳维持 |

---

## 🚀 快速开始

### 环境要求
- Windows 10/11 + Python 3.8+
- 闲鱼 / 淘宝 / 拼多多 卖家账号
- DeepSeek API Key（免费注册）

### 3 步上手

```bash
# 第 1 步：一键部署
双击 setup.bat               # 自动装好所有依赖

# 第 2 步：配置
编辑 config\.env              # 填入 API_KEY + 平台 Cookie
编辑 config\platforms.json    # 开启/关闭平台

# 第 3 步：启动
双击 start.bat                # 开始自动值守！
```

---

## 🏗️ 项目结构

```
UniversalECommerceBot/
├── main.py                    # 🎯 主程序 - 多平台启动器
├── core/                      # 🧠 核心引擎
│   ├── base_platform.py       #   平台接口（实现即接入）
│   ├── agent.py               #   LLM 多 Agent 系统
│   └── context.py             #   数据库管理
├── platforms/                 # 🔌 平台实现
│   ├── xianyu/                #   ✅ 闲鱼（已跑通）
│   │   ├── bot.py             #     连接 + 消息 + 发货
│   │   └── api.py             #     API 封装
│   └── template.py            #   📝 新平台模板
├── config/                    # ⚙️ 配置
│   ├── .env.example
│   ├── platforms.json         #   平台开关
│   └── delivery_items.json    #   自动发货配置
├── prompts/                   # 📝 LLM 提示词
├── utils/                     # 🔧 工具函数
├── setup.bat                  # ⚡ 一键部署
├── start.bat                  # ▶️ 启动脚本
└── requirements.txt
```

---

## 🔌 接入新平台

实现 `core/base_platform.py` 中的 5 个方法即可：

```python
from core.base_platform import BasePlatform

class MyPlatformBot(BasePlatform):
    async def connect(self): ...
    async def disconnect(self): ...
    async def start(self, handler): ...
    async def send_message(self, user_id, msg): ...
    async def send_delivery(self, user_id, item_id, info): ...
    async def get_item_info(self, item_id): ...
```

然后在 `config/platforms.json` 中注册：

```json
{
  "my_platform": {
    "enabled": true,
    "name": "我的平台",
    "class": "platforms.my_platform.bot.MyPlatformBot",
    "config": {}
  }
}
```

---

## 📖 自动发货配置

```json
{
  "items": {
    "商品ID": {
      "name": "资料名称",
      "link": "https://pan.baidu.com/s/xxxxx",
      "code": "提取码",
      "note": "使用说明"
    }
  }
}
```

---

## 📄 开源协议

MIT License

---

<div align="center">
<p>如果你觉得这个项目有用，Star ⭐ 支持！</p>
</div>
