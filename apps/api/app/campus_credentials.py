"""Opt-in campus credential vault, encrypted with the OS user's DPAPI key.

Design rules:
- Credentials live only in the local secret store (Windows DPAPI, current-user
  bound; the same mechanism as provider API keys). They are never returned by any
  API, never written to logs/journals/events, and never placed in model context.
- Storing them is a deliberate user choice. Without stored credentials the product
  keeps its previous behaviour: one manual login, then a persisted session.
- Automatic re-login is best effort. Captcha or multi-factor challenges cannot be
  satisfied by the backend, so the caller must fall back to a single manual login
  and keep the task checkpoint instead of retrying in a loop.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import secrets_store

SCOPES = ("passport", "webvpn")
_STORE_PREFIX = "campus-"
_MAX = {"username": 64, "password": 256, "authcode": 32}


class CredentialInputError(ValueError):
    """Invalid credential payload; never echoes the supplied value."""


def _store_id(scope: str) -> str:
    if scope not in SCOPES:
        raise CredentialInputError("未知的凭据用途")
    return _STORE_PREFIX + scope


def _clean(value, field: str, *, allow_empty=False) -> str:
    if not isinstance(value, str):
        raise CredentialInputError("凭据字段必须是文本")
    text = value.strip() if field == "username" else value
    if not text and not allow_empty:
        raise CredentialInputError("凭据字段不能为空")
    if len(text) > _MAX[field]:
        raise CredentialInputError("凭据字段过长")
    if any(ord(char) < 32 for char in text):
        raise CredentialInputError("凭据字段不能包含控制字符")
    return text


def save(scope: str, *, username: str, password: str, authcode: str = "",
         auto_relogin: bool = True) -> dict:
    """Encrypt and store one scope's credentials; returns non-secret status."""
    payload = {
        "username": _clean(username, "username"),
        "password": _clean(password, "password"),
        "authcode": _clean(authcode, "authcode", allow_empty=True),
        "auto_relogin": bool(auto_relogin),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    secrets_store.set_key(_store_id(scope), json.dumps(payload, ensure_ascii=False))
    return status(scope)


def load(scope: str) -> dict | None:
    """Trusted-backend only. Callers must never log, return or model-forward this."""
    raw = secrets_store.get_key(_store_id(scope))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not value.get("username") or not value.get("password"):
        return None
    return {"username": str(value["username"]), "password": str(value["password"]),
            "authcode": str(value.get("authcode") or ""),
            "auto_relogin": bool(value.get("auto_relogin", True)),
            "saved_at": str(value.get("saved_at") or "")}


def configured(scope: str) -> bool:
    return secrets_store.has_key(_store_id(scope))


def mask(username: str) -> str:
    """Show only enough to recognize the account; never the full identifier."""
    if len(username) <= 2:
        return username[:1] + "***"
    if len(username) <= 5:
        return username[0] + "***" + username[-1]
    return username[:2] + "***" + username[-2:]


def status(scope: str) -> dict:
    """Owner-facing summary. Contains no credential material."""
    stored = load(scope)
    return {
        "scope": scope,
        "configured": stored is not None,
        "username_hint": mask(stored["username"]) if stored else None,
        "auto_relogin": bool(stored and stored["auto_relogin"]),
        "saved_at": stored["saved_at"] if stored else None,
        "note": "凭据仅以本机 DPAPI 加密保存；不会进入对话、日志或模型上下文。",
    }


def set_auto_relogin(scope: str, enabled: bool) -> dict:
    """Toggle automatic re-login without re-entering the password."""
    stored = load(scope)
    if stored is None:
        raise CredentialInputError("尚未保存该用途的凭据")
    secrets_store.set_key(_store_id(scope), json.dumps({
        "username": stored["username"], "password": stored["password"],
        "authcode": stored["authcode"], "auto_relogin": bool(enabled),
        "saved_at": stored["saved_at"] or datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False))
    return status(scope)


def clear(scope: str) -> dict:
    """Idempotent removal; returns the post-clear status."""
    secrets_store.clear_key(_store_id(scope))
    return status(scope)


def all_status() -> dict:
    return {"items": [status(scope) for scope in SCOPES],
            "dpapi": os.name == "nt",
            "residual_risk": "学校要求验证码或二次认证时，自动登录无法完成，会退回一次人工登录；"
                             "此功能不能绕过校方访问控制。"}
