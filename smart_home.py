#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Home Aggregator — 统一智能家居调度引擎
==============================================
同时查询/控制小米米家 + Aqara 设备，合并输出。

用法:
    python smart_home.py list                    # 列出所有设备
    python smart_home.py list --room 客厅          # 按房间过滤
    python smart_home.py list --type WindowCovering # 按设备类型过滤
    python smart_home.py find "窗帘"              # 跨平台搜索
    python smart_home.py status --name "阳台窗帘"   # 查询设备状态
    python smart_home.py control --name "阳台窗帘" --action close  # 控制设备
    python smart_home.py control --name "客厅空调" --action set_temp --value 26
    python smart_home.py --refresh list           # 强制刷新（不用缓存）
    python smart_home.py --json list              # JSON 输出

平台路由:
    - 米家设备 (纯数字 DID) → mijiaAPI 直调
    - Aqara 设备 (Aqr~ 前缀) → aqara_open_api.py CLI (subprocess)

Windows 编码策略:
    Aqara CLI 输出始终重定向到临时文件，再用 utf-8 读取。
    绝不通过管道传递 — GBK 编码会截断中文设备名。
"""

import sys
import os
import json
import argparse
import threading
import subprocess
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

# ============================================================
# 路径配置 — 自动发现
# ============================================================
SKILLS_ROOT = Path.home() / ".workbuddy" / "skills"
CACHE_DIR = Path(__file__).resolve().parent / "config"
CACHE_FILE = CACHE_DIR / "devices_cache.json"
CACHE_TTL = 300  # 5 分钟

# 米家路径
MIJIA_SKILL = SKILLS_ROOT / "xiaomi-home-agent"
MIJIA_AUTH = MIJIA_SKILL / "config" / "auth.json"

# Aqara 路径
AQARA_SKILL = SKILLS_ROOT / "aqara-agent"
AQARA_CLI = AQARA_SKILL / "scripts" / "aqara_open_api.py"
AQARA_ACCOUNT = AQARA_SKILL / "assets" / "user_account.json"

# 如果米家 auth 不在 skill config，尝试 workspace 路径
_MIJIA_AUTH_ALT = Path.home() / "WorkBuddy" / "智能家居控制" / ".workbuddy" / "mijia-auth" / "auth.json"

# Python 解释器
PYTHON = sys.executable


# Windows UTF-8 stdout/stderr
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ============================================================
# 依赖检查 — 子技能存在性扫描
# ============================================================
def check_dependencies() -> dict:
    """
    扫描 xiaomi-home-agent 和 aqara-agent 是否已安装。
    
    返回:
        {
            "ok": bool,                # 全部就绪
            "missing": ["米家", ...],  # 缺失的平台名
            "xiaomi": {"installed": bool, "path": str},
            "aqara":  {"installed": bool, "path": str},
        }
    """
    result = {
        "ok": True,
        "missing": [],
        "xiaomi": {"installed": False, "path": str(MIJIA_SKILL)},
        "aqara":  {"installed": False, "path": str(AQARA_SKILL)},
    }

    # 检查米家技能
    if MIJIA_SKILL.exists() and (MIJIA_SKILL / "SKILL.md").exists():
        result["xiaomi"]["installed"] = True
    else:
        result["ok"] = False
        result["missing"].append("米家 (xiaomi-home-agent)")

    # 检查 Aqara 技能
    if AQARA_SKILL.exists() and (AQARA_SKILL / "SKILL.md").exists():
        result["aqara"]["installed"] = True
    else:
        result["ok"] = False
        result["missing"].append("Aqara (aqara-agent)")

    return result


def _format_dependency_prompt(dep: dict) -> str:
    """当子技能缺失时，生成可操作的选择提示"""
    missing = dep["missing"]
    lines = [
        "",
        "=" * 56,
        "⚠️  Smart Home Aggregator — 缺少底层平台技能",
        "=" * 56,
        "",
    ]
    for m in missing:
        lines.append(f"   未安装: {m}")

    lines += [
        "",
        "请选择安装方案：",
        "",
        "  1. 都安装        — 安装米家和 Aqara 两个技能",
        "  2. 安装 Aqara    — 只安装 aqara-agent（Aqara 官方）",
        "  3. 安装米家      — 只安装 xiaomi-home-agent",
        "  4. 都不安装      — 跳过，仅使用已有平台（功能受限）",
        "",
        "安装方式（按优先级）：",
        "  ① 通过 WorkBuddy 技能市场搜索安装",
        "  ② 市场搜不到时，用 GitHub 仓库兜底：",
        "     Aqara:  https://github.com/aqara/aqara-agent-skills （Aqara 官方）",
        "     米家:   https://github.com/xahao512/xiaomi-home-agent",
        "=" * 56,
    ]
    return "\n".join(lines)


# ============================================================
# 工具函数
# ============================================================
def _get_mijia_auth() -> Optional[Path]:
    """返回有效的米家认证文件路径"""
    for p in [MIJIA_AUTH, _MIJIA_AUTH_ALT]:
        if p.exists():
            return p
    return None


def _safe_json_load(filepath: Path) -> Optional[dict]:
    """安全读取 JSON 文件"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ============================================================
# 米家适配层
# ============================================================
class XiaomiAdapter:
    """米家平台查询"""

    def __init__(self):
        self.auth = _get_mijia_auth()
        self.available = self.auth is not None

    def list_devices(self) -> dict:
        if not self.available:
            return {"platform": "米家", "ok": False, "error": "未登录", "devices": []}

        try:
            from mijiaAPI import mijiaAPI

            api = mijiaAPI(auth_data_path=str(self.auth))
            raw = api.get_devices_list()
            devices = []
            for d in raw:
                devices.append({
                    "name":       d.get("name", "?"),
                    "model":      d.get("model", "?"),
                    "online":     bool(d.get("isOnline", False)),
                    "room":       d.get("room_name", "-"),
                    "did":        d.get("did", "?"),
                    "type":       "",  # 米家没有标准 type 字段
                    "platform":   "米家",
                })
            return {"platform": "米家", "ok": True, "count": len(devices), "devices": devices}
        except Exception as e:
            return {"platform": "米家", "ok": False, "error": str(e), "devices": []}

    def get_status(self, did: str) -> dict:
        """查询单个设备属性状态"""
        if not self.available:
            return {"ok": False, "error": "未登录"}

        try:
            from mijiaAPI import mijiaAPI

            api = mijiaAPI(auth_data_path=str(self.auth))
            # 尝试常见 siid/piid 组合
            props = [
                {"did": did, "siid": 2, "piid": 1},  # 开关
                {"did": did, "siid": 2, "piid": 2},  # 位置/亮度
                {"did": did, "siid": 2, "piid": 3},  # 目标位置/温度
                {"did": did, "siid": 2, "piid": 4},  # 方向
            ]
            result = api.get_devices_prop(props)
            status = {}
            for r in result:
                if r.get("code") == 0:
                    status[f"siid{r.get('siid')}_piid{r.get('piid')}"] = r.get("value")
            return {"ok": True, "status": status}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def control(self, did: str, action: str, value: Any = None) -> dict:
        """控制米家设备"""
        if not self.available:
            return {"ok": False, "error": "米家未登录"}

        try:
            from mijiaAPI import mijiaAPI

            api = mijiaAPI(auth_data_path=str(self.auth))

            # 通用属性控制
            if action in ("on", "off"):
                data = [{"did": did, "siid": 2, "piid": 1, "value": action == "on"}]
                result = api.set_devices_prop(data)
                return {"ok": result and result[0].get("code") in (0, 1), "result": result}

            elif action == "set_position":
                # 开窗器/窗帘位置控制
                val = int(value) if value is not None else 50
                data = [{"did": did, "siid": 2, "piid": 2, "value": val}]
                result = api.set_devices_prop(data)
                return {"ok": result and result[0].get("code") in (0, 1), "result": result}

            elif action == "set_temp":
                val = int(value) if value is not None else 26
                data = [{"did": did, "siid": 2, "piid": 3, "value": val}]
                result = api.set_devices_prop(data)
                return {"ok": result and result[0].get("code") in (0, 1), "result": result}

            elif action == "set_brightness":
                val = int(value) if value is not None else 50
                data = [{"did": did, "siid": 2, "piid": 2, "value": val}]
                result = api.set_devices_prop(data)
                return {"ok": result and result[0].get("code") in (0, 1), "result": result}

            return {"ok": False, "error": f"不支持的动作: {action}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ============================================================
# Aqara 适配层
# ============================================================
class AqaraAdapter:
    """Aqara 平台查询 — 通过 CLI subprocess，文件 I/O 避 GBK 坑"""

    def __init__(self):
        self.account = _safe_json_load(AQARA_ACCOUNT) if AQARA_ACCOUNT.exists() else None
        self.available = self.account is not None and bool(self.account.get("aqara_api_key"))
        self.cli = str(AQARA_CLI) if AQARA_CLI.exists() else None

    def _run(self, tool: str, payload: dict = None) -> dict:
        """执行 Aqara CLI 命令，stdout 写文件再解析"""
        if not self.available or not self.cli:
            return {"code": -1, "message": "Aqara 未配置"}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            out_file = f.name

        args_list = [json.dumps(payload)] if payload else []

        try:
            env = os.environ.copy()
            env["AQARA_OPEN_HOST"] = "agent.aqara.com"
            env["PYTHONIOENCODING"] = "utf-8"

            proc = subprocess.run(
                [PYTHON, self.cli, tool] + args_list,
                capture_output=True,
                timeout=45,
                cwd=str(AQARA_SKILL),
                env=env,
            )
            # 先尝试从 stdout 解码
            raw = proc.stdout
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 回退：写临时文件再读
                pass
        except subprocess.TimeoutExpired:
            return {"code": -1, "message": "Aqara 超时"}
        except Exception as e:
            return {"code": -1, "message": str(e)}

        # 回退路径：写文件再读
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(proc.stdout.decode("utf-8", errors="replace"))
            with open(out_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"code": -1, "message": "JSON 解析失败"}
        finally:
            try:
                os.unlink(out_file)
            except OSError:
                pass

    def _run_to_file(self, tool: str, payload: dict = None) -> Path:
        """运行 Aqara CLI，stdout 重定向到临时文件"""
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        out_file = tmp.name
        tmp.close()

        args_list = [json.dumps(payload)] if payload else []

        env = os.environ.copy()
        env["AQARA_OPEN_HOST"] = "agent.aqara.com"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            with open(out_file, "w", encoding="utf-8") as f:
                subprocess.run(
                    [PYTHON, self.cli, tool] + args_list,
                    stdout=f,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                    cwd=str(AQARA_SKILL),
                    env=env,
                )
        except subprocess.TimeoutExpired:
            # 写入超时标记
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump({"code": -1, "message": "超时"}, f)

        return Path(out_file)

    def list_devices(self) -> dict:
        if not self.available:
            return {"platform": "Aqara", "ok": False, "error": "未配置 API Key", "devices": []}

        out_file = self._run_to_file("get_home_devices")
        data = _safe_json_load(out_file)
        try:
            os.unlink(out_file)
        except OSError:
            pass

        if not data or data.get("code") != 0:
            return {"platform": "Aqara", "ok": False, "error": data.get("message", "API 错误") if data else "无响应", "devices": []}

        rows = data.get("result", [])
        if len(rows) < 2:
            return {"platform": "Aqara", "ok": True, "count": 0, "devices": []}

        header = rows[0]
        devices = []
        for row in rows[1:]:
            # 格式: [endpoint_id, endpoint_name, device_name, device_type, position_name, position_id]
            devices.append({
                "name":       row[1] if len(row) > 1 else "?",
                "model":      row[2] if len(row) > 2 else "?",
                "online":     True,  # Aqara 列表不含在线状态，需 post_device_status
                "room":       row[4] if len(row) > 4 else "-",
                "did":        row[0] if len(row) > 0 else "?",
                "type":       row[3] if len(row) > 3 else "",
                "platform":   "Aqara",
            })
        return {"platform": "Aqara", "ok": True, "count": len(devices), "devices": devices}

    def get_status(self, did: str) -> dict:
        """查询 Aqara 设备在线/状态"""
        if not self.available:
            return {"ok": False, "error": "未配置"}

        result = self._run("post_device_status", {"device_ids": [did]})
        if result.get("code") != 0:
            return {"ok": False, "error": result.get("message", "查询失败")}

        rows = result.get("result", [])
        if len(rows) < 2:
            return {"ok": False, "error": "无数据"}

        status = {}
        for row in rows[1:]:
            # [endpoint_id, endpoint_name, device_name, device_type, position_name, position_id, status]
            raw_status = row[6] if len(row) > 6 else "{}"
            try:
                # status 是 Python dict 的字符串表示
                parsed = eval(raw_status) if isinstance(raw_status, str) else raw_status
                status = {**status, **parsed}
            except Exception:
                status["raw"] = str(raw_status)
        return {"ok": True, "status": status}

    def control(self, did: str, action: str, value: Any = None, dev_type: str = "") -> dict:
        """控制 Aqara 设备 — 通过 post_device_control"""
        if not self.available:
            return {"ok": False, "error": "Aqara 未配置"}

        # 动作映射到 Aqara attribute/action/value
        attr, act, val = self._map_action(action, value, dev_type)
        if attr is None:
            return {"ok": False, "error": f"不支持的控制动作: {action} (设备类型: {dev_type})"}

        payload = {
            "device_id": did,
            "attribute": attr,
            "action": act,
        }
        if val is not None:
            payload["value"] = str(val)

        result = self._run("post_device_control", payload)
        return {"ok": result.get("code") == 0, "result": result}

    @staticmethod
    def _map_action(action: str, value: Any, dev_type: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """将统一动作映射为 Aqara attribute/action/value"""
        DT = dev_type

        # 通用开关
        if action == "on":
            return ("on_off", "on", None)
        if action == "off":
            return ("on_off", "off", None)

        # 窗帘/窗户/晾衣架 — 位置控制
        if action == "open" and DT in ("WindowCovering", "ClotheDryingMachine"):
            return ("percentage", "set", "100")
        if action == "close" and DT in ("WindowCovering", "ClotheDryingMachine"):
            return ("percentage", "set", "0")
        if action == "set_position" and DT in ("WindowCovering", "ClotheDryingMachine"):
            return ("percentage", "set", str(value or 50))
        if action == "stop" and DT in ("WindowCovering", "ClotheDryingMachine"):
            return ("motion", "set", "stop")

        # 空调
        if action == "set_temp" and DT == "AirConditioner":
            return ("temperature", "set", str(value or 26))
        if action == "ac_cool" and DT == "AirConditioner":
            return ("ac_mode", "set", "cool")
        if action == "ac_heat" and DT == "AirConditioner":
            return ("ac_mode", "set", "heat")
        if action == "ac_dry" and DT == "AirConditioner":
            return ("ac_mode", "set", "dry")
        if action == "ac_fan" and DT == "AirConditioner":
            return ("ac_mode", "set", "fan")
        if action == "ac_auto" and DT == "AirConditioner":
            return ("ac_mode", "set", "auto")

        # 灯光
        if action == "set_brightness" and DT == "Light":
            return ("brightness", "set", str(value or 50))
        if action == "set_color_temp" and DT == "Light":
            return ("color_temperature", "set", str(value or 5000))

        return (None, None, None)


# ============================================================
# 缓存
# ============================================================
def _load_cache() -> Optional[dict]:
    data = _safe_json_load(CACHE_FILE)
    if not data:
        return None
    ts = data.get("_ts", 0)
    if time.time() - ts > CACHE_TTL:
        return None
    return data


def _save_cache(data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["_ts"] = time.time()
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 核心：聚合查询
# ============================================================
def query_all(refresh: bool = False) -> dict:
    """并行查询两个平台，返回合并设备列表"""
    if not refresh:
        cached = _load_cache()
        if cached:
            return cached

    xiaomi = XiaomiAdapter()
    aqara = AqaraAdapter()

    results: Dict[str, dict] = {}

    def _run(label: str, fn):
        results[label] = fn()

    threads = [
        threading.Thread(target=_run, args=("xiaomi", xiaomi.list_devices)),
        threading.Thread(target=_run, args=("aqara", aqara.list_devices)),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=50)

    x_res = results.get("xiaomi", {"ok": False, "error": "线程超时", "devices": []})
    a_res = results.get("aqara", {"ok": False, "error": "线程超时", "devices": []})

    all_devices = x_res.get("devices", []) + a_res.get("devices", [])
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(all_devices),
        "xiaomi": {"ok": x_res.get("ok", False), "count": x_res.get("count", 0), "error": x_res.get("error")},
        "aqara":  {"ok": a_res.get("ok", False), "count": a_res.get("count", 0), "error": a_res.get("error")},
        "devices": all_devices,
    }
    _save_cache(output)
    return output


# ============================================================
# 格式化输出
# ============================================================
ONLINE = {True: "🟢", False: "🔴"}
TYPE_ICONS = {
    "WindowCovering": "🪟", "Light": "💡", "AirConditioner": "❄️",
    "Sensor": "📊", "Switch": "🔘", "Button": "🔳", "Outlet": "🔌",
    "SweepingRobot": "🧹", "Speaker": "🔊", "ClotheDryingMachine": "👕",
}
DEVICE_TYPE_CN = {
    "WindowCovering": "窗帘/窗", "Light": "灯", "AirConditioner": "空调",
    "Sensor": "传感器", "Switch": "开关", "Button": "按钮", "Outlet": "插座",
    "SweepingRobot": "扫地机", "Speaker": "音箱", "ClotheDryingMachine": "晾衣架",
}


def _fmt_device(d: dict, show_did: bool = False) -> str:
    icon = ONLINE.get(d.get("online"), "⚪")
    dtype = d.get("type", "")
    type_icon = TYPE_ICONS.get(dtype, "")
    type_cn = DEVICE_TYPE_CN.get(dtype, dtype) or ""
    platform = d.get("platform", "?")
    plat_tag = "[米家]" if platform == "米家" else "[Aqara]"
    did_part = f"  did={d['did']}" if show_did else ""
    return f"  {icon} {type_icon} {plat_tag} {d['name']:<20s} | {d['room']:<6s} | {type_cn}{did_part}"


def format_table(data: dict, room: str = None, dtype: str = None, show_did: bool = False):
    """文本表格输出"""
    devices = data.get("devices", [])
    # 过滤
    if room:
        devices = [d for d in devices if room in d.get("room", "")]
    if dtype:
        devices = [d for d in devices if dtype.lower() in d.get("type", "").lower()]

    # 按平台+房间排序
    devices.sort(key=lambda d: (d.get("platform", ""), d.get("room", ""), d.get("name", "")))

    online_n = sum(1 for d in devices if d.get("online"))
    offline_n = len(devices) - online_n

    lines = [
        f"=== 智能家居设备总览 (共 {len(devices)} 个，🟢{online_n} 在线 / 🔴{offline_n} 离线) ===",
        f"     米家: {data['xiaomi'].get('count', 0)} 个  |  Aqara: {data['aqara'].get('count', 0)} 个",
        f"     查询时间: {data.get('timestamp', '-')[:19]}",
        "",
    ]

    # 按房间分组
    rooms: Dict[str, list] = {}
    for d in devices:
        r = d.get("room", "-")
        rooms.setdefault(r, []).append(d)

    for r in sorted(rooms.keys()):
        devs = rooms[r]
        lines.append(f"## {r} ({len(devs)} 个)")
        for d in devs:
            lines.append(_fmt_device(d, show_did))
        lines.append("")

    # 平台统计
    x_ok = data["xiaomi"].get("ok", False)
    a_ok = data["aqara"].get("ok", False)
    if not x_ok or not a_ok:
        lines.append("---")
        if not x_ok:
            lines.append(f"⚠️  米家不可用: {data['xiaomi'].get('error', '未知')}")
        if not a_ok:
            lines.append(f"⚠️  Aqara 不可用: {data['aqara'].get('error', '未知')}")

    return "\n".join(lines)


def format_find(devices: list, keyword: str):
    """搜索结果输出"""
    if not devices:
        return f"❌ 未找到匹配「{keyword}」的设备"

    lines = [f"🔍 搜索「{keyword}」— {len(devices)} 个匹配:", ""]
    for d in devices:
        lines.append(_fmt_device(d, show_did=True))
    return "\n".join(lines)


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Smart Home Aggregator — 统一智能家居调度",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python smart_home.py list
  python smart_home.py list --room 客厅
  python smart_home.py find 窗帘
  python smart_home.py status --name "阳台窗帘"
  python smart_home.py control --name "阳台窗帘" --action close
  python smart_home.py control --name "客厅空调" --action set_temp --value 26
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # check — 依赖扫描
    p_check = sub.add_parser("check", help="检查底层平台技能是否就绪")

    # list
    p_list = sub.add_parser("list", help="列出所有设备")
    p_list.add_argument("--room", type=str, help="按房间过滤")
    p_list.add_argument("--type", type=str, help="按设备类型过滤 (WindowCovering/Light/AirConditioner/...)")

    # find
    p_find = sub.add_parser("find", help="跨平台搜索设备")
    p_find.add_argument("keyword", type=str, help="搜索关键词")

    # status
    p_stat = sub.add_parser("status", help="查询设备详细状态")
    p_stat.add_argument("--name", type=str, required=True, help="设备名称")
    p_stat.add_argument("--did", type=str, help="设备 ID（可选，优先使用）")

    # control
    p_ctrl = sub.add_parser("control", help="控制设备")
    p_ctrl.add_argument("--name", type=str, required=True, help="设备名称")
    p_ctrl.add_argument("--action", type=str, required=True,
                        help="动作: on/off/open/close/set_temp/set_brightness/set_position/stop")
    p_ctrl.add_argument("--value", type=str, help="动作参数值")
    p_ctrl.add_argument("--dev-type", type=str, help="设备类型（可选，帮助路由）")

    # 全局参数
    parser.add_argument("--refresh", action="store_true", help="强制刷新，不使用缓存")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--show-did", action="store_true", help="显示设备 ID")

    args = parser.parse_args()

    # ── 启动时依赖检查 ──
    if args.command != "check":
        dep = check_dependencies()
        if not dep["ok"]:
            if args.json:
                print(json.dumps(dep, ensure_ascii=False, indent=2))
            else:
                print(_format_dependency_prompt(dep))
            sys.exit(1)

    if args.command == "check":
        dep = check_dependencies()
        if args.json:
            print(json.dumps(dep, ensure_ascii=False, indent=2))
        elif dep["ok"]:
            print("✅ 所有底层平台技能已就绪")
            print(f"   米家: {'✅' if dep['xiaomi']['installed'] else '❌'} {dep['xiaomi']['path']}")
            print(f"   Aqara:  {'✅' if dep['aqara']['installed'] else '❌'} {dep['aqara']['path']}")
        else:
            print(_format_dependency_prompt(dep))

    elif args.command == "list":
        data = query_all(refresh=args.refresh)
        if args.room or args.type:
            devices = data.get("devices", [])
            if args.room:
                devices = [d for d in devices if args.room in d.get("room", "")]
            if args.type:
                devices = [d for d in devices if args.type.lower() in d.get("type", "").lower()]
            data["devices"] = devices
            data["total"] = len(devices)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(format_table(data, room=args.room, dtype=args.type, show_did=args.show_did))

    elif args.command == "find":
        data = query_all(refresh=args.refresh)
        kw = args.keyword.lower()
        matches = [d for d in data.get("devices", [])
                   if kw in d.get("name", "").lower()
                   or kw in d.get("type", "").lower()
                   or kw in d.get("room", "").lower()]
        if args.json:
            print(json.dumps(matches, ensure_ascii=False, indent=2))
        else:
            print(format_find(matches, args.keyword))

    elif args.command == "status":
        # 先找到设备属于哪个平台
        data = query_all(refresh=args.refresh)
        target = None
        if args.did:
            target = next((d for d in data.get("devices", []) if d["did"] == args.did), None)
        if not target:
            matches = [d for d in data.get("devices", []) if args.name in d.get("name", "")]
            target = matches[0] if matches else None

        if not target:
            print(f"❌ 未找到设备: {args.name}")
            sys.exit(1)

        plat = target["platform"]
        did = target["did"]
        print(f"🔍 查询 [{plat}] {target['name']} (did={did})")

        if plat == "米家":
            adapter = XiaomiAdapter()
        else:
            adapter = AqaraAdapter()

        status = adapter.get_status(did)
        print(json.dumps(status, ensure_ascii=False, indent=2))

    elif args.command == "control":
        # 先找到设备
        data = query_all(refresh=args.refresh)
        matches = [d for d in data.get("devices", []) if args.name in d.get("name", "")]
        if not matches:
            print(f"❌ 未找到设备: {args.name}")
            sys.exit(1)

        if len(matches) > 1:
            print(f"⚠️  找到 {len(matches)} 个匹配设备，将操作第一个:")
            for m in matches:
                print(_fmt_device(m, show_did=True))
            print()

        target = matches[0]
        plat = target["platform"]
        did = target["did"]
        dev_type = args.dev_type or target.get("type", "")

        print(f"🎛️  [{plat}] {target['name']} → {args.action}" + (f" {args.value}" if args.value else ""))

        if plat == "米家":
            adapter = XiaomiAdapter()
        else:
            adapter = AqaraAdapter()

        result = adapter.control(did, args.action, args.value, dev_type)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("ok"):
            print("✅ 操作成功")
        else:
            print(f"❌ 操作失败: {result.get('error', '未知错误')}")


if __name__ == "__main__":
    main()
