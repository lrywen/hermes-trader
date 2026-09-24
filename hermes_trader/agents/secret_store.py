"""统一敏感配置（secret）存储：LLM 模型 / 交易所 API / 飞书。

设计要点
--------
- 与普通交易配置（``.agent-config.json``）**物理分离**：密钥只落在此文件，
  路径默认 ``.secrets.json``（可用 ``HERMES_SECRETS_FILE`` 覆盖），并加入
  ``.gitignore``。
- 文件权限强制 ``0600``（属主可读写，其他人不可访问）；每次写走
  ``atomic_io.write_json_atomic``（tmp+fsync+replace），不会撕裂或截断。
- 结构（版本化）::

    {
      "version": 1,
      "llm": {
         "profiles": {
            "<id>": {"id","name","base_url","api_key","model",
                     "temperature","max_tokens","timeout_sec","enabled":bool}
         },
         "default_profile_id": "<id>"
      },
      "exchange": {"testnet":false,"wallet_address","master_address","private_key"},
      "feishu":  {"base_url","webhook_url","webhook_secret",
                  "signal_webhook_url","signal_webhook_secret",
                  "non_trade_webhook_url","non_trade_webhook_secret",
                  "notify_categories"}
    }

- 永远不在读取 API 中返回明文密钥：``masked_view()`` 输出掩码；编辑时密钥
  字段留空（``""``）表示“不修改”，重填即整体替换。
- ``bootstrap_env()`` 在进程启动早期（``.env.local`` 加载之后）把已保存的
  secret 注入 ``os.environ``，**沿用 setdefault**：真实环境变量 / K8s Secret
  优先级最高，secret 文件只作补充，因此完全兼容现有读取代码。

本模块不依赖 FastAPI，纯函数 + 文件锁，方便单测与脚本复用。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from typing import Any, Optional

from hermes_trader.agents.atomic_io import write_json_atomic

logger = logging.getLogger("hermes-secrets")

# ── 路径 ────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
SECRETS_PATH = os.environ.get(
    "HERMES_SECRETS_FILE", os.path.join(_PROJECT_ROOT, ".secrets.json")
)

_VERSION = 1
_lock = threading.RLock()

_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ── 默认结构 ────────────────────────────────────────────────────────────
def _empty_doc() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "llm": {"profiles": {}, "default_profile_id": None},
        "exchange": {
            "testnet": False,
            "wallet_address": "",
            "master_address": "",
            "private_key": "",
        },
        "feishu": {
            "base_url": "",
            "webhook_url": "",
            "webhook_secret": "",
            "signal_webhook_url": "",
            "signal_webhook_secret": "",
            "non_trade_webhook_url": "",
            "non_trade_webhook_secret": "",
            "notify_categories": "",
        },
    }


# ── 落盘读写 ────────────────────────────────────────────────────────────
def _read_raw() -> dict[str, Any]:
    """读取并规范化 secret 文档；文件缺失/损坏时返回空结构（不抛错）。"""
    doc = _empty_doc()
    try:
        with open(SECRETS_PATH, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return doc
    except (json.JSONDecodeError, OSError) as e:
        logger.error("[secrets] unreadable (%s) — using empty view, NOT overwriting blindly: %s",
                     SECRETS_PATH, e)
        return doc
    if isinstance(raw, dict):
        # 只做浅合并三段，保留已识别字段
        for section in ("llm", "exchange", "feishu"):
            if isinstance(raw.get(section), dict):
                merged = dict(doc[section])
                merged.update({k: v for k, v in raw[section].items() if k in doc[section]})
                doc[section] = merged
        # profiles 单独合并
        profiles = raw.get("llm", {}).get("profiles", {})
        if isinstance(profiles, dict):
            doc["llm"]["profiles"] = {
                k: v for k, v in profiles.items() if isinstance(v, dict)
            }
    return doc


def _write_raw(doc: dict[str, Any]) -> None:
    """原子写并强制 0600 权限。"""
    write_json_atomic(SECRETS_PATH, doc, indent=2, fsync=True, ebusy_fallback=True)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        # Windows / 某些不支持 chmod 的文件系统：原子写已完成，权限尽力而为。
        pass


def read_secrets() -> dict[str, Any]:
    """返回完整 secret 文档（含明文，仅供后端内部使用）。"""
    with _lock:
        return _read_raw()


# ── 掩码 ────────────────────────────────────────────────────────────────
def mask(value: Any, keep: int = 4) -> str:
    """把敏感字符串掩码为 ``前缀***末4位``；空值返回空串。"""
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return f"{s[:3]}***{s[-keep:]}" if len(s) > keep + 3 else f"***{s[-keep:]}"


_SECRET_LEAVES = {
    "exchange": {"private_key"},
    "feishu": {
        "webhook_secret",
        "signal_webhook_secret",
        "non_trade_webhook_secret",
    },
}
# LLM 每个 profile 中的密钥叶子
_LLM_SECRET_LEAF = "api_key"


def masked_view() -> dict[str, Any]:
    """输出供前端展示的视图：所有密钥字段掩码，其余原样。"""
    doc = read_secrets()
    out = copy.deepcopy(doc)
    for section, leaves in _SECRET_LEAVES.items():
        for leaf in leaves:
            out[section][leaf] = mask(out[section].get(leaf))
    for pid, prof in out["llm"]["profiles"].items():
        if isinstance(prof, dict):
            prof[_LLM_SECRET_LEAF] = mask(prof.get(_LLM_SECRET_LEAF))
    return out


# ── 校验（返回错误字符串列表；空列表=通过） ─────────────────────────────
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _validate_llm_profile(p: dict[str, Any], *, partial: bool) -> list[str]:
    errs: list[str] = []
    if not partial or p.get("name") is not None:
        if not str(p.get("name", "")).strip():
            errs.append("模型名称不能为空")
    if not partial or p.get("base_url") is not None:
        bu = str(p.get("base_url", ""))
        if not bu:
            if not partial:
                errs.append("Base URL 不能为空")
        elif not _URL_RE.match(bu):
            errs.append("Base URL 必须以 http(s):// 开头")
    if not partial or p.get("model") is not None:
        if not str(p.get("model", "")).strip():
            errs.append("模型标识 model 不能为空")
    if not partial or p.get("api_key") is not None:
        if not partial and not str(p.get("api_key", "")).strip():
            errs.append("API Key 不能为空")
    for nkey in ("temperature",):
        if p.get(nkey) is not None:
            try:
                v = float(p[nkey])
                if not (0 <= v <= 2):
                    errs.append("temperature 须在 0–2 之间")
            except (TypeError, ValueError):
                errs.append("temperature 必须是数字")
    for nkey in ("max_tokens",):
        if p.get(nkey) is not None:
            try:
                if int(p[nkey]) < 1:
                    errs.append("max_tokens 须 ≥ 1")
            except (TypeError, ValueError):
                errs.append("max_tokens 必须是整数")
    for nkey in ("timeout_sec",):
        if p.get(nkey) is not None:
            try:
                if float(p[nkey]) <= 0:
                    errs.append("timeout_sec 须 > 0")
            except (TypeError, ValueError):
                errs.append("timeout_sec 必须是数字")
    return errs


def _validate_exchange(p: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    pk = str(p.get("private_key", ""))
    if pk and not _HEX_RE.match(pk):
        errs.append("交易所私钥须为 64 位十六进制（可带 0x 前缀）")
    for f in ("wallet_address", "master_address"):
        v = str(p.get(f, ""))
        if v and not _ADDR_RE.match(v):
            errs.append(f"{f} 须为 0x 开头的 40 位地址")
    return errs


def _validate_feishu(p: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for f in (
        "webhook_url",
        "signal_webhook_url",
        "non_trade_webhook_url",
    ):
        v = str(p.get(f, ""))
        if v and not _URL_RE.match(v):
            errs.append(f"{f} 必须以 http(s):// 开头")
    cats = str(p.get("notify_categories", ""))
    if cats:
        bad = [c.strip() for c in cats.split(",") if c.strip() and not re.match(r"^[a-z_]+$", c.strip())]
        if bad:
            errs.append(f"notify_categories 含非法类别: {','.join(bad)}")
    return errs


# ── CRUD：LLM profiles（多实例） ───────────────────────────────────────
def _clean_id(value: str) -> str:
    return _ID_RE.sub("", value) or "profile"


def list_llm_profiles() -> list[dict[str, Any]]:
    doc = read_secrets()
    default_id = doc["llm"].get("default_profile_id")
    out = []
    for pid, prof in doc["llm"]["profiles"].items():
        item = copy.deepcopy(prof)
        item["id"] = pid
        item["is_default"] = pid == default_id
        item["api_key"] = mask(prof.get("api_key"))
        out.append(item)
    out.sort(key=lambda x: x.get("name", ""))
    return out


def create_llm_profile(data: dict[str, Any]) -> tuple[Optional[str], list[str]]:
    """新建 profile，返回 (id, errors)。"""
    errs = _validate_llm_profile(data, partial=False)
    if errs:
        return None, errs
    with _lock:
        doc = _read_raw()
        base_id = _clean_id(str(data.get("id") or data.get("name", "profile")))
        pid = base_id
        n = 2
        while pid in doc["llm"]["profiles"]:
            pid = f"{base_id}-{n}"
            n += 1
        prof = {
            "id": pid,
            "name": str(data["name"]).strip(),
            "base_url": str(data["base_url"]).strip().rstrip("/"),
            "api_key": str(data["api_key"]).strip(),
            "model": str(data["model"]).strip(),
            "temperature": float(data.get("temperature", 0.1)),
            "max_tokens": int(data.get("max_tokens", 500)),
            "timeout_sec": float(data.get("timeout_sec", 25.0)),
            "enabled": bool(data.get("enabled", True)),
        }
        doc["llm"]["profiles"][pid] = prof
        # 第一个 profile 自动成为默认
        if not doc["llm"].get("default_profile_id"):
            doc["llm"]["default_profile_id"] = pid
        _write_raw(doc)
    return pid, []


def update_llm_profile(pid: str, data: dict[str, Any]) -> list[str]:
    """编辑已有 profile。

    ``api_key`` 留空/缺失表示不修改原密钥；提供非空值则整体替换。
    ``name`` 等可空字符串字段同样按提交内容更新。
    """
    errs = _validate_llm_profile(data, partial=True)
    with _lock:
        doc = _read_raw()
        prof = doc["llm"]["profiles"].get(pid)
        if prof is None:
            return ["profile 不存在"]
        if errs:
            return errs
        field_map = {
            "name": lambda v: str(v).strip(),
            "base_url": lambda v: str(v).strip().rstrip("/"),
            "model": lambda v: str(v).strip(),
            "temperature": float,
            "max_tokens": int,
            "timeout_sec": float,
            "enabled": bool,
        }
        for k, cast in field_map.items():
            if k in data and data[k] != "":
                prof[k] = cast(data[k])  # type: ignore[operator]
        new_key = str(data.get("api_key", ""))
        if new_key:
            prof["api_key"] = new_key.strip()
        doc["llm"]["profiles"][pid] = prof
        _write_raw(doc)
    return []


def delete_llm_profile(pid: str) -> list[str]:
    with _lock:
        doc = _read_raw()
        if pid not in doc["llm"]["profiles"]:
            return ["profile 不存在"]
        del doc["llm"]["profiles"][pid]
        if doc["llm"].get("default_profile_id") == pid:
            remaining = sorted(doc["llm"]["profiles"].keys())
            doc["llm"]["default_profile_id"] = remaining[0] if remaining else None
        _write_raw(doc)
    return []


def set_default_llm_profile(pid: str) -> list[str]:
    with _lock:
        doc = _read_raw()
        if pid not in doc["llm"]["profiles"]:
            return ["profile 不存在"]
        doc["llm"]["default_profile_id"] = pid
        _write_raw(doc)
    return []


# ── CRUD：exchange / feishu（单实例整块编辑） ──────────────────────────
def update_singleton(section: str, data: dict[str, Any]) -> list[str]:
    """更新 exchange 或 feishu 单实例。

    约定：
    - 密钥字段（private_key / *_secret）提交空串 = 不修改；
    - 非密钥字段以提交值为准（允许清空，传空串即清空）。
    """
    if section not in ("exchange", "feishu"):
        return ["unknown section"]
    with _lock:
        doc = _read_raw()
        cur = doc[section]
        secret_leaves = _SECRET_LEAVES[section]
        if section == "exchange" and "testnet" in data:
            cur["testnet"] = bool(data["testnet"])
        for k in list(cur.keys()):
            if k == "testnet" or k not in data:
                continue
            v = data[k]
            if k in secret_leaves:
                v = str(v)
                if v == "":
                    continue  # 留空=保留原密钥
                cur[k] = v.strip()
            else:
                cur[k] = str(v).strip()
        errs = (
            _validate_exchange(cur) if section == "exchange" else _validate_feishu(cur)
        )
        if errs:
            return errs
        doc[section] = cur
        _write_raw(doc)
    return []


# ── 启动注入：secret → os.environ ──────────────────────────────────────
# 叶子 -> 目标环境变量
_EXCHANGE_ENV_MAP = {
    "wallet_address": "HYPERLIQUID_WALLET_ADDRESS",
    "master_address": "HYPERLIQUID_MASTER_ADDRESS",
    "private_key": "HYPERLIQUID_PRIVATE_KEY",
}
_FEISHU_ENV_MAP = {
    "base_url": "HERMES_BASE_URL",
    "webhook_url": "FEISHU_WEBHOOK_URL",
    "webhook_secret": "FEISHU_WEBHOOK_SECRET",
    "signal_webhook_url": "FEISHU_SIGNAL_WEBHOOK_URL",
    "signal_webhook_secret": "FEISHU_SIGNAL_WEBHOOK_SECRET",
    "non_trade_webhook_url": "FEISHU_NON_TRADE_WEBHOOK_URL",
    "non_trade_webhook_secret": "FEISHU_NON_TRADE_WEBHOOK_SECRET",
    "notify_categories": "FEISHU_NOTIFY_CATEGORIES",
}


def _setdefault_nonempty(key: str, value: Any) -> None:
    s = str(value or "").strip()
    if s:
        os.environ.setdefault(key, s)


def bootstrap_env() -> None:
    """把已保存 secret 注入环境变量（在 ``.env.local`` 之后调用）。

    使用 ``setdefault``：真实环境变量 / K8s Secret 优先，secret 文件补齐，
    不覆盖既有值。LLM 默认 profile 同时映射到 ``OPENROUTER_*`` 与当前生效
    模型，使现有 research/notify 代码零改动即可用。
    """
    try:
        doc = _read_raw()
    except Exception as e:  # 绝不能阻断启动
        logger.debug("[secrets] bootstrap skipped: %s", e)
        return

    # exchange
    ex = doc["exchange"]
    for leaf, env in _EXCHANGE_ENV_MAP.items():
        _setdefault_nonempty(env, ex.get(leaf))

    # feishu
    fe = doc["feishu"]
    for leaf, env in _FEISHU_ENV_MAP.items():
        _setdefault_nonempty(env, fe.get(leaf))

    # LLM 默认 profile
    llm = doc["llm"]
    pid = llm.get("default_profile_id")
    prof = llm["profiles"].get(pid) if pid else None
    if isinstance(prof, dict):
        _setdefault_nonempty("OPENROUTER_API_KEY", prof.get("api_key"))
        _setdefault_nonempty("OPENROUTER_BASE_URL", prof.get("base_url"))
        _setdefault_nonempty("OPENROUTER_MODEL", prof.get("model"))
