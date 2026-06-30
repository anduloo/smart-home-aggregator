---
name: Smart Home Aggregator
version: 1.2.2
description: Unified smart home aggregation across Xiaomi Mijia and Aqara platforms.
triggers:
  - 查看所有设备
  - 智能家居设备
  - 家里有哪些设备
  - 查看智能家居
  - 开/关 + 设备名（跨平台）
  - 搜索/查找/找 + 设备名（跨平台）
---

# Smart Home Aggregator

统一调度小米米家 + Aqara 两个平台的设备查询与控制。

底层平台 Skill：`xiaomi-home-agent`（米家）、`aqara-agent`（Aqara）。
本 Skill 是**聚合层**，不替代它们，而是在它们之上提供统一入口。

## 核心脚本

`smart_home.py` — 调度引擎，位于本 Skill 目录。

| 命令 | 用途 |
|------|------|
| `smart_home.py check` | 扫描 aqara-agent / xiaomi-home-agent 是否已安装 |
| `smart_home.py list` | 列出所有设备（并行查询米家 + Aqara） |
| `smart_home.py list --room 客厅` | 按房间过滤 |
| `smart_home.py list --type WindowCovering` | 按设备类型过滤 |
| `smart_home.py find 关键词` | 跨平台搜索设备名 |
| `smart_home.py status --name "设备名"` | 查单个设备状态 |
| `smart_home.py control --name "设备名" --action close` | 控制设备 |
| `smart_home.py --json list` | JSON 输出 |

## AI 行为规则

### 1. 查询规则

- 用户说「查看所有设备」「家里有哪些设备」→ **直接运行 `smart_home.py list`**，不加载子 skill
- 用户说「查找 XX 设备」「有没有 XX」→ 运行 `smart_home.py find XX`
- 用户明确指定平台（「只看米家」「只看 Aqara」）→ 降级加载对应子 skill

### 2. 首次加载规则（依赖检查）

**每次加载本 skill 时，必须先运行 `smart_home.py check` 扫描子技能状态。**

- 如果 `ok: true` → 子技能全部就绪，继续正常流程
- 如果 `ok: false` → 输出缺失平台列表，**向用户呈现安装选项**：

```
请选择安装方案：
  1. 都安装        — 安装米家和 Aqara 两个技能
  2. 安装 Aqara    — 只安装 aqara-agent
  3. 安装米家      — 只安装 xiaomi-home-agent
  4. 都不安装      — 跳过，仅使用已有平台（功能受限）
```

- 用户选择后，优先通过 `marketplace-skill-installer` 搜索安装
- **兜底方案**：如果 marketplace 搜索不到，直接用以下 GitHub 仓库安装：

| 平台 | GitHub 仓库 | 来源 |
|------|-----------|------|
| Aqara | `https://github.com/aqara/aqara-agent-skills` | **Aqara 官方** |
| 米家 | `https://github.com/xahao512/xiaomi-home-agent` | 社区（xahao512） |

- 安装完成后重新运行 `smart_home.py check` 确认就绪

### 3. 控制规则

- 先用 `smart_home.py find` 定位设备
- 根据返回的 `platform` 字段路由到正确平台：
  - `米家` → 调用子 skill 的 `control_device.py` 或直接用 mijiaAPI
  - `Aqara` → 调用子 skill 的 `aqara_open_api.py post_device_control`
- 设备名模糊匹配找到多个时，展示候选让用户确认

### 4. Windows 编码规则（Aqara 专用）

- **绝不**通过管道传递 Aqara CLI 输出（Windows GBK 会截断 UTF-8）
- 正确方式：
  1. `aqara_open_api.py get_home_devices > tmp.json`（stdout 重定向到文件）
  2. Python `open(tmp.json, encoding='utf-8')` 读取

### 5. 米家 mijiaAPI 登录排错

**⚠️ 常见问题：QR 码登录多次失败**

| 症状 | 原因 | 解决方案 |
|------|------|----------|
| `mijiaAPI -l` 无响应或报错 | 沙箱环境缺少交互式终端 | 不能在沙箱中直接运行交互命令 |
| 生成的 QR 码图片无法扫描 | 终端渲染的 QR 码不完整 | 使用 Python 直接生成 QR 码 PNG 图片再扫描 |
| 扫码后超时（login_result.txt = TIMEOUT） | 小米 OAuth 长轮询超时 | 需要在有效期内完成扫码（通常 5 分钟） |
| 多次尝试都失败 | 旧的 session/ticket 残留 | 清理后重新获取 login URL |

**✅ 正确的登录流程：**

1. 用 Python 调用 `mijiaAPI` **非交互方式**获取登录 URL：
   ```python
   from mijiaAPI import mijiaAPI
   api = mijiaAPI()
   login_url = api.get_login_url()
   ```
2. 将 URL 保存为 QR 码 PNG 文件（`qrcode` + `pillow`）：
   ```python
   import qrcode
   img = qrcode.make(login_url)
   img.save('config/qr_code.png')
   ```
3. 打开 `config/qr_code.png`，用**米家 APP** 扫码授权
4. 扫码后等待 3-5 秒，用 Python 检查登录状态：
   ```python
   api = mijiaAPI(auth_data_path='config/auth.json')
   devices = api.get_devices_list()
   print(f'已登录，{len(devices)} 个设备')
   ```
5. 成功后 `auth.json` 自动保存到 `~/.workbuddy/skills/xiaomi-home-agent/config/auth.json`
6. 之后 `mijiaAPI(auth_data_path=...)` 会自动复用 Token，**无需重复扫码**

**⚠️ 注意事项：**
- QR 码有效期约 5 分钟，超时需重新生成
- `mijiaAPI -l`（命令行交互模式）在 WorkBuddy 沙箱中**不可用**
- Token 过期（约 30 天）后需重新扫码，届时 `get_devices_list()` 会抛异常
- QR 码文件 (`qr_code.png`) 和登录 URL (`qr_login_url.txt`) 登录成功后可以删除

### 6. 跨平台设备去重

- 米家设备 DID 是纯数字，如 `1117327919`
- Aqara 设备 DID 以 `Aqr~` 开头，如 `Aqr~pjsFLW3...`
- 某些设备可能同时接入两个平台（如网关），查询结果应标注平台

### 7. 缓存规则

- `smart_home.py` 自带 5 分钟缓存
- 用户如果短时间内连续查询 → 直接用缓存结果
- 用户说「刷新」或做控制操作后 → 加 `--refresh` 重新查询

## 依赖

- Python 3.8+
- `mijiaAPI`（米家，pip install mijiaAPI）
- Aqara 需先通过 `aqara-agent` skill 配置 API Key 和家庭

## 故障处理

| 症状 | 处理 |
|------|------|
| 米家返回 0 设备 | 检查 `~/.workbuddy/skills/xiaomi-home-agent/config/auth.json` 是否存在 |
| 米家 API 抛认证异常 | Token 过期（约 30 天），需重新扫码登录（见规则 4） |
| 米家 QR 码登录超时 | 在 5 分钟内完成扫码；过期后重新生成 QR 码 |
| `mijiaAPI -l` 报错/无响应 | 这是交互命令，WorkBuddy 沙箱不支持；改用规则 4 的 Python 非交互方式 |
| 米家传感器查不到属性数据 | **PIID 盲区**：BLE/人体存在传感器属性 ID 在 1000+（如 Linptect ES3 的 occupancy-status=piid 1078），`get_status()` 已覆盖标准 + BLE 双范围，详见 xiaomi-home-agent SKILL.md § BLE/传感器 PIID 范围 |
| Aqara 返回 0 设备 | 检查 `assets/user_account.json` 的 API Key 和 home_id |
| Aqara 输出乱码 | 使用文件重定向方式（见规则 3） |
| 控制失败 | 先 `status` 确认设备在线，再检查控制参数 |

## 与子技能的关系

```
smart-home-aggregator (本 Skill)
    ├── 自动路由米家请求 → xiaomi-home-agent
    └── 自动路由 Aqara 请求 → aqara-agent

优势:
  - 单平台操作可直接加载子 skill（更轻量）
  - 子 skill 独立 git pull 更新，互不影响
  - 聚合层只做路由和格式化
```
