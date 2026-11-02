# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2", "requests"]
# ///
"""AI 平台额度查询 MCP 服务端。

把 quota-panel 桌面组件的取数能力封装成 MCP 工具，Claude Code 这类客户端在对话里就能问「各家 AI 平台的额度还剩多少」。配置直接复用 quota-panel 的 config.json 与同目录的 *.token 令牌文件，两边改一处、两边生效。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

SERVER_DIR = Path(__file__).resolve().parent
CACHE_TTL_SECONDS = 60
TIMEOUT_SECONDS = 8

# 对话里连续追问很常见（「再看看」「刚才那个呢」），TTL 内共享一次取数结果，
# 避免每次追问都真打一遍平台接口
_cache: dict[str, Any] = {"at": 0.0, "rows": None}

_week_start_weekday = {"monday": 0, "sunday": 6, "saturday": 5}

# 报错里出现完整地址时替换掉 —— 令牌写在查询串里的话，地址就是令牌
_BARE_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _config_path() -> Path:
    """配置文件在哪：环境变量指定 > 本目录 > 旁挂的 quota-panel 目录。

    旁挂兜底让「clone 到 quota-panel 旁边」这种布局零配置可用；
    布局不同就用 QUOTA_MCP_CONFIG 指过去。
    """
    env = os.environ.get("QUOTA_MCP_CONFIG")
    if env:
        return Path(env)
    local = SERVER_DIR / "config.json"
    if local.exists():
        return local
    return SERVER_DIR.parent / "quota-panel" / "config.json"


def _read_secret(config_dir: Path, name: str) -> str:
    """读令牌文件；读不到给空串，让请求带着空令牌自然失败并报出认证错误。

    @param name 只取文件名部分 —— 配置里写 ../.. 也读不到别处的文件
    """
    path = config_dir / Path(str(name)).name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_kv(text: str, config_dir: Path, coerce=None) -> dict:
    """把「每行一条 名字: 值」拆成字典。

    值以 @ 开头时改读同目录那个文件（令牌不进 config.json）；
    其余先按 JSON 解析、解析不了当字符串。

    @param coerce 逐值转换函数；请求头必须传 str，数字型头会让 requests 报错
    """
    out: dict[str, Any] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value.startswith("@"):
            value = _read_secret(config_dir, value[1:].strip())
        else:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        out[key] = coerce(value) if coerce else value
    return out


# ── 取值表达式（与 quota-panel 同一套语法）─────────────────────────

_TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _tokens(path: str) -> list[str]:
    """把 a.b[0].c 切成 ['a','b','0','c']；a[] 这种空方括号切成通配符 '[]'。"""
    out, buf = [], ""
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            if buf:
                out.append(buf)
                buf = ""
        elif ch == "[":
            if buf:
                out.append(buf)
                buf = ""
            end = path.find("]", i)
            if end == -1:
                break
            inner = path[i + 1 : end].strip()
            out.append(inner if inner else "[]")
            i = end
        elif ch in _TOKEN_CHARS:
            buf += ch
        i += 1
    if buf:
        out.append(buf)
    return out


def _dig(nodes: list, toks: list) -> list:
    """沿剩下的 token 逐层展开；通配符 '[]' 把整个列表摊平进结果集。"""
    if not toks:
        return nodes
    tok, rest = toks[0], toks[1:]
    nxt = []
    for node in nodes:
        if tok == "[]":
            if isinstance(node, list):
                nxt.extend(node)
        elif isinstance(node, dict):
            if tok in node:
                nxt.append(node[tok])
        elif isinstance(node, list) and tok.isdigit():
            idx = int(tok)
            if 0 <= idx < len(node):
                nxt.append(node[idx])
    return _dig(nxt, rest)


def _as_number(value) -> float | None:
    """能被当成数字就转成 float，否则 None。

    NaN 和 Infinity 不算数 —— 它们能顺利通过 float()，但拿去算百分比会得出 nan%。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def pick(root, expr) -> float | None:
    """按表达式从返回内容里取一个数；取不到返回 None。

    支持 sum: 前缀把匹配到的一批值相加；表达式是纯数字时直接当常量（固定上限用）。
    """
    expr = (expr or "").strip()
    if not expr:
        return None
    if _as_number(expr) is not None and not expr.startswith("sum:"):
        return float(expr)
    total = expr.startswith("sum:")
    if total:
        expr = expr[4:].strip()
    numbers = [n for n in (_as_number(v) for v in _dig([root], _tokens(expr))) if n is not None]
    if not numbers:
        return None
    return sum(numbers) if total else numbers[0]


# ── 周期与基线（读 quota-panel 的记账，只读不写）────────────────────

def _period_key(provider, now=None) -> str:
    """算当前周期的标识串；周期一变基线自然作废。"""
    now = now or datetime.now()
    kind = (provider.get("period") or "none").lower()
    if kind == "week":
        start = _week_start_weekday.get((provider.get("week_start") or "monday").lower(), 0)
        back = (now.weekday() - start) % 7
        return (now - timedelta(days=back)).strftime("%Y-%m-%d")
    if kind == "month":
        return now.strftime("%Y-%m")
    return "all"


def _load_baselines(config_dir: Path) -> dict:
    """读 quota-panel 的 cache.json 拿周期起点。

    只读不写 —— 桌面组件是基线的记账人，这里只是对账的；
    写的话两边各记一套，同一周的「已用」会算出两个数。
    """
    try:
        data = json.loads((config_dir / "cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    baselines = data.get("baselines") if isinstance(data, dict) else None
    return baselines if isinstance(baselines, dict) else {}


def _apply_baseline(provider, used: float, baselines: dict):
    """累计值换算成本周期增量；起点缺失时退回原值并标注。

    @return (换算后的值, 是否缺起点)
    """
    saved = baselines.get(provider.get("id"))
    if not isinstance(saved, dict) or saved.get("period") != _period_key(provider):
        return used, True
    try:
        return used - float(saved.get("value")), False
    except (TypeError, ValueError):
        return used, True


# ── 请求 ──────────────────────────────────────────────────────────

def _request_json(provider, config_dir: Path):
    """按配置发一次请求并返回解析好的 JSON；出错抛异常，由调用方转成人话。"""
    req = provider.get("request") or {}
    url = (req.get("url") or "").strip()
    if not url:
        raise ValueError("地址没填")
    headers = _parse_kv(req.get("headers"), config_dir, coerce=str)
    params = _parse_kv(req.get("params"), config_dir)
    method = (req.get("method") or "GET").upper()
    kwargs = {"headers": headers or None, "timeout": TIMEOUT_SECONDS}
    if (req.get("where") or "query") == "body":
        kwargs["json"] = params or None
    else:
        kwargs["params"] = params or None
    # 默认不探测系统代理：注册表里的残留代理会让请求报「Please check proxy URL」
    # 这种看不出所以然的错（代理软件卸载不干净就长这样）
    session = requests.Session()
    session.trust_env = bool(req.get("use_system_proxy"))
    try:
        response = session.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()
    finally:
        session.close()


def _friendly_error(exc) -> str:
    """把异常翻成一句话，让人不用看 traceback 就知道下一步该干嘛。"""
    if isinstance(exc, requests.exceptions.Timeout):
        return "连不上（超时）"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "连不上（网络不通或地址不对）"
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        return {
            401: "key 不对或已过期",
            403: "没有访问权限",
            404: "地址不对（找不到）",
        }.get(code, f"服务端返回 {code}")
    if isinstance(exc, json.JSONDecodeError):
        return "返回的不是 JSON"
    if isinstance(exc, ValueError):
        return str(exc)
    # 兜底分支会把异常原文带出来，而 requests 有些异常的文字里含完整地址；
    # 令牌若写在查询串里，这一行就会把令牌打进对话
    return _BARE_URL.sub("(地址已隐去)", f"出错：{exc}")[:100]


# ── 组装成一行结果 ────────────────────────────────────────────────

def _base_row(provider) -> dict:
    """一行结果的骨架；只读配置不碰返回内容，所以不会抛。"""
    display = provider.get("display") or {}
    return {
        "name": provider.get("name") or "未命名",
        "kind": (display.get("kind") or "percent").lower(),
        "unit": display.get("unit") or "",
        "ok": False,
        "error": None,
        "used": None,
        "limit": None,
        "percent": None,
        "note": None,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _compute(provider, raw, baselines) -> dict:
    """把一次原始返回算成一行结果；异常收进行里，不往上抛。"""
    row = _base_row(provider)
    extract = provider.get("extract") or {}
    used = pick(raw, extract.get("used"))
    if used is None:
        row["error"] = "没取到「已用」那个数"
        return row
    if provider.get("baseline"):
        used, missing = _apply_baseline(provider, used, baselines)
        if missing:
            row["note"] = "周期起点缺失，显示的是累计原值"
    row["used"] = used
    if row["kind"] == "amount":
        row["ok"] = True
        return row
    limit = pick(raw, extract.get("limit"))
    if not limit:
        row["error"] = "没取到「上限」那个数"
        return row
    row["limit"] = limit
    row["percent"] = used / limit * 100
    row["ok"] = True
    return row


def _fetch_one(provider, config_dir: Path, baselines) -> dict:
    """取一个平台的数；任何异常都收进结果里，不往上抛。

    整个函数体都包在 try 里 —— 不只是发请求，算数那一步也会抛（取值表达式、周期基线换算）。
    漏出去会顺着调用方的列表推导炸掉整批查询，后续平台根本没轮到。
    """
    try:
        return _compute(provider, _request_json(provider, config_dir), baselines)
    except Exception as exc:
        row = _base_row(provider)
        row["error"] = _friendly_error(exc)
        return row


def fetch_all(force: bool = False) -> list[dict]:
    """取所有启用平台的数；TTL 内重复调用共享同一次结果。"""
    now = time.monotonic()
    if not force and _cache["rows"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["rows"]
    path = _config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("配置不是一个 JSON 对象")
    except (OSError, ValueError) as exc:
        # OSError 的文字里自带完整路径，报给用户只需要文件名
        reason = exc.strerror if isinstance(exc, OSError) else str(exc)
        return [{
            "name": "(配置)", "kind": "percent", "unit": "", "ok": False,
            "error": f"读不了配置 {path.name}：{reason}", "used": None,
            "limit": None, "percent": None, "note": None,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }]
    config_dir = path.parent
    baselines = _load_baselines(config_dir)
    providers = [
        p for p in config.get("providers", [])
        if isinstance(p, dict) and p.get("enabled", True)
    ]
    rows = [_fetch_one(p, config_dir, baselines) for p in providers]
    _cache.update(at=now, rows=rows)
    return rows


def _summary_table(rows: list[dict]) -> str:
    """结果排成 markdown 表 —— MCP 客户端能直接渲染，人眼也能读。"""
    lines = [
        "| 平台 | 类型 | 已用 / 余额 | 上限 | 剩余 | 占比 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        if not r["ok"]:
            lines.append(f"| {r['name']} | - | ❌ {r['error']} | - | - | - |")
        elif r["kind"] == "amount":
            lines.append(f"| {r['name']} | 余额 | {r['unit']}{r['used']:,.2f} | - | - | - |")
        else:
            mark = "⚠️" if r["percent"] >= 70 else "✅"
            lines.append(
                f"| {r['name']} | 占比 | {mark} {r['used']:,.2f}{r['unit']} | "
                f"{r['limit']:,.2f}{r['unit']} | {r['limit'] - r['used']:,.2f}{r['unit']} | {r['percent']:.1f}% |"
            )
    lines.append("")
    lines.append(f"取数时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


# ── MCP 工具 ─────────────────────────────────────────────────────

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("quota-mcp")


@mcp.tool()
def get_quota_summary() -> str:
    """查询所有已配置 AI 平台的额度与余额，返回汇总表（占比型：已用/上限/剩余/占比；余额型：当前值）。取数失败的平台会标注原因。"""
    return _summary_table(fetch_all())


@mcp.tool()
def get_platform_detail(platform: str) -> str:
    """查单个平台的额度明细（含失败原因与原始数值）。platform 填平台名或其中一段（大小写不敏感，如 jsx6、DeepSeek）。"""
    rows = fetch_all()
    key = (platform or "").strip().lower()
    hits = [r for r in rows if key in r["name"].lower()]
    if not hits:
        names = "、".join(r["name"] for r in rows)
        return f"没有叫「{platform}」的平台。当前配置的有：{names}"
    parts = []
    for r in hits:
        if not r["ok"]:
            parts.append(f"{r['name']}：取数失败 —— {r['error']}")
            continue
        if r["kind"] == "amount":
            parts.append(f"{r['name']}：当前余额 {r['unit']}{r['used']:,.2f}（{r['at']}）")
        else:
            parts.append(
                f"{r['name']}：已用 {r['used']:,.2f}{r['unit']} / 上限 {r['limit']:,.2f}{r['unit']}，"
                f"剩余 {r['limit'] - r['used']:,.2f}{r['unit']}，占比 {r['percent']:.1f}%（{r['at']}）"
            )
        if r.get("note"):
            parts.append(f"  注：{r['note']}")
    return "\n".join(parts)


def main() -> None:
    # Windows 控制台/重定向默认 GBK，表里的表情符号打不出去；统一转 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AI 平台额度查询 MCP 服务端")
    parser.add_argument("--once", action="store_true", help="不进 MCP 循环，取一次数打成表格打印（调试 / 演示用）")
    parser.add_argument("--force", action="store_true", help="跳过 60 秒缓存，强制重新取数")
    args = parser.parse_args()
    if args.once:
        print(_summary_table(fetch_all(force=args.force)))
        return
    mcp.run()


if __name__ == "__main__":
    main()
