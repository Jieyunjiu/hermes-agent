"""
Multi-tenant isolation primitives for the Hermes gateway.

This module implements the *single owner key* model defined by the multi-tenant
rebuild plan (study/multi-tenant-wecom-rebuild-plan.md, 必改 1). Every isolation
surface in phase 1 — routing, history reads, session resume, memory, workspace,
uploaded attachments, and tool-surface convergence — MUST key off the same
``owner_key`` so that data belonging to different WeCom users is isolated by
construction.

What lives here
---------------
* ``build_owner_key``        — construct the canonical owner key from the
                                corp/app/user identity triple.
* ``hash_owner_key``         — collision-resistant, path-safe directory name
                                derived from an owner key.
* ``get_current_owner_key``  — read the per-request owner key from the gateway
                                ContextVar. **fail-closed** under multi-tenancy.
* ``multi_tenant_enabled``   — whether the gateway is operating in multi-tenant
                                mode (``security.multi_tenant.enabled``).
* ``assert_session_owner``   — unified helper that enforces "the target session
                                belongs to the current owner" at every history /
                                resume entry point. Recommended by 必改 3 & 9 to
                                avoid hand-written (and therefore leak-prone)
                                per-entry-point checks.
* ``owner_workspace_root``   — the on-disk workspace root for an owner
                                (``/data/workspaces/<owner_hash>`` by default).

Design rules (from the plan, do not violate)
--------------------------------------------
1. **fail-closed**: under multi-tenancy, a missing owner key is never treated
   as "global" — the request is refused. Only the non-multi-tenant path (CLI,
   cron, single-user gateway) tolerates an absent owner key.
2. **anti-enumeration**: an owner check that fails must report "session not
   found", never "forbidden" — leaking the *existence* of another user's
   session lets an attacker enumerate ids/titles.
3. **one key, everywhere**: never mix ``owner_key`` with the platform's raw
   ``user_id``. Two different corps can each have a user called "zhangsan";
   the ``corp_id`` (and ``app_id``) in the key disambiguate them.
"""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


MULTI_TENANT_RELOAD_DENIED = (
    "Blocked in multi-tenant gateway mode: dynamic tool/skill reload is disabled "
    "so one user cannot change the model tool surface for other users. Ask an "
    "operator to update the shared deployment configuration instead."
)


_WECOM_MULTI_TENANT_TOOLSET = "wecom_multi_tenant"

# ---------------------------------------------------------------------------
# ContextVar: the per-request owner key (必改 2 — injected via copy_context)
# ---------------------------------------------------------------------------
# Sentinel mirroring gateway/session_context.py: a distinct object means
# "never set in this context" (so we can fall back / decide fail-closed),
# while the empty string "" means "explicitly cleared".
_UNSET: object = object()

_OWNER_KEY: ContextVar = ContextVar("HERMES_OWNER_KEY", default=_UNSET)


# ---------------------------------------------------------------------------
# owner_key construction (必改 1)
# ---------------------------------------------------------------------------

def build_owner_key(corp_id: str, app_id: str, user_id: str) -> str:
    """Build the canonical multi-tenant owner key.

    Format: ``wecom:<corp_id>:<app_id>:<user_id>``
    e.g. ``wecom:ww1234:1000001:zhangsan``

    The three-part identity is what makes the key globally unique across
    corps and apps: two enterprises that both have a user named "zhangsan"
    produce different keys because their ``corp_id`` differs. ``app_id`` is
    included so that two self-built apps inside the same corp (e.g. an
    internal-tools app and an HR app) get separate data even if they share
    a user namespace.

    All three components are coerced to stripped strings; an empty
    ``user_id`` yields an empty owner key (the caller treats that as
    "unknown owner" and, under multi-tenancy, refuses the request).
    """
    corp = (corp_id or "").strip()
    app = (app_id or "").strip()
    user = (user_id or "").strip()
    if not user:
        return ""
    return f"wecom:{corp}:{app}:{user}"


def hash_owner_key(owner_key: str) -> str:
    """Return a path-safe, collision-resistant directory name for an owner key.

    Uses ``sha256(owner_key)[:16]`` (16 hex chars). This both minimizes the
    chance of a directory-name collision and prevents path-traversal /
    path-injection via a malicious owner key (the hash output is a fixed
    hex alphabet, so it can never contain ``..`` or ``/``).
    """
    if not owner_key:
        return ""
    return hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Multi-tenancy mode + owner-key ContextVar accessors
# ---------------------------------------------------------------------------

def _config_multi_tenant() -> dict:
    """Read the ``security.multi_tenant`` config block (best-effort).

    The gateway config is loaded lazily and may be unavailable in some code
    paths (CLI, tests). We treat any failure as "multi-tenancy disabled" —
    i.e. the legacy single-user behavior — so non-gateway callers are never
    broken by a config read error.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        sec = cfg.get("security", {}) or {}
        mt = sec.get("multi_tenant", {}) or {}
        return mt if isinstance(mt, dict) else {}
    except Exception:
        return {}


def multi_tenant_enabled() -> bool:
    """Whether the gateway is operating in multi-tenant isolation mode.

    Driven by ``security.multi_tenant.enabled`` in config.yaml. When False
    (the default), the gateway behaves exactly as before — owner keys are
    not required, and every isolation primitive degrades to the legacy
    global behavior. When True, every per-user surface is partitioned by
    the current owner key and a missing owner key is fail-closed.
    """
    return bool(_config_multi_tenant().get("enabled", False))


def set_current_owner_key(owner_key: str) -> None:
    """Bind the owner key for the current async task / executor thread.

    Called from ``_set_session_env`` (gateway/run.py) so the value flows
    into the agent worker thread via ``copy_context()``. Setting "" marks
    the context as explicitly-cleared (no owner), which is distinct from
    the never-set sentinel.
    """
    _OWNER_KEY.set(owner_key or "")


def clear_current_owner_key() -> None:
    """Mark the owner key as explicitly cleared (no fallback)."""
    _OWNER_KEY.set("")


@contextmanager
def scoped_owner_key(owner_key: str):
    """临时绑定 owner_key，并在退出时恢复调用方原来的上下文。

    网关里有少数 slash command 会在 ``_set_session_env`` 之前执行，例如
    ``/memory pending`` 和 ``/skills pending``。这些命令同样会读取 owner
    隔离后的 pending/memory 目录，所以需要一个很小的作用域工具：进入时
    绑定当前消息的 owner，退出时恢复原值，避免影响同一 async task 后续逻辑。
    """
    previous = _OWNER_KEY.get()
    set_current_owner_key(owner_key)
    try:
        yield
    finally:
        _OWNER_KEY.set(previous)


def get_current_owner_key() -> str:
    """Return the owner key for the current request context.

    **fail-closed under multi-tenancy** (必改 1): when multi-tenant mode is
    on and no owner key is present (never set, or explicitly ""), this
    raises :class:`OwnerKeyMissing`. Callers that perform isolation must
    not swallow this — it means the request cannot be safely partitioned.

    When multi-tenancy is *off*, a missing key simply returns "" so legacy
    single-user code paths keep working.
    """
    value = _OWNER_KEY.get()
    if value is _UNSET:
        value = ""
    if multi_tenant_enabled() and not value:
        # fail-closed: refuse rather than fall back to a shared namespace.
        raise OwnerKeyMissing(
            "multi-tenant mode is enabled but no owner key is bound to this "
            "context; refusing the request to avoid cross-user data exposure"
        )
    return value


def current_owner_key_or_none() -> Optional[str]:
    """Like :func:`get_current_owner_key` but returns ``None`` on missing.

    Use this in non-isolation code paths that merely *prefer* an owner key
    (e.g. tagging log lines) but must not raise. Isolation-critical code
    MUST use the fail-closed :func:`get_current_owner_key`.
    """
    try:
        return get_current_owner_key()
    except OwnerKeyMissing:
        return None


class OwnerKeyMissing(RuntimeError):
    """Raised when multi-tenancy is on but no owner key is available.

    Carries no sensitive data — safe to surface in logs / responses."""


def constrain_toolsets_for_owner(
    enabled_toolsets: Optional[list[str]],
    disabled_toolsets: Optional[list[str]],
    *,
    owner_key: str,
) -> tuple[Optional[list[str]], Optional[list[str]]]:
    """Return the runtime toolset policy for an owner-scoped gateway turn.

    多租户阶段 1 的目标是先把安全边界闭环，而不是继承个人助手的完整工具面。
    因此一旦当前消息带有 owner_key，就不再相信平台配置、MCP 动态刷新或
    plugin toolset 注入出来的工具列表，而是强制收敛到一个固定 allowlist：
    ``wecom_multi_tenant``。这个 toolset 不包含 terminal/process/execute_code/
    delegate_task/skill_manage/cronjob/browser/computer_use 等高风险工具。

    ``disabled_toolsets`` 在这里清空，是因为 allowlist 本身已经是最终工具面；
    再叠加旧的禁用项可能把必需的 memory/session/file 工具误删。
    """
    if multi_tenant_enabled() and owner_key:
        return [_WECOM_MULTI_TENANT_TOOLSET], None
    return enabled_toolsets, disabled_toolsets


def dynamic_reload_denied_message() -> str:
    """统一返回多租户模式下动态 reload 的拒绝文案。"""
    return MULTI_TENANT_RELOAD_DENIED


def sandbox_config() -> dict:
    """返回 security.multi_tenant.sandbox 配置块（缺失返回空 dict）。"""
    sb = _config_multi_tenant().get("sandbox", {}) or {}
    return sb if isinstance(sb, dict) else {}


def sandbox_enabled() -> bool:
    """多租户开启 且 sandbox.enabled=true 才为真。"""
    return bool(multi_tenant_enabled() and sandbox_config().get("enabled", False))


# ---------------------------------------------------------------------------
# Workspace root for an owner (必改 7 & 8 share this anchor)
# ---------------------------------------------------------------------------

def owner_workspace_root(owner_key: str) -> Path:
    """Return the on-disk workspace root for an owner.

    Defaults to ``<workspace_root>/<owner_hash>`` where ``workspace_root`` is
    ``security.multi_tenant.workspace_root`` (default ``/data/workspaces``).
    Uploaded attachments (必改 8) land under ``<root>/uploads/...`` and the
    file tools (必改 7) confine reads/writes to ``<root>``. The hashed
    component guarantees the path can never escape via traversal —
    :func:`hash_owner_key` emits only hex digits.
    """
    mt = _config_multi_tenant()
    base = str(mt.get("workspace_root", "/data/workspaces")).strip() or "/data/workspaces"
    owner_hash = hash_owner_key(owner_key)
    if not owner_hash:
        # No owner key -> no workspace. Callers in multi-tenant mode will
        # have already failed closed upstream; this branch only matters for
        # the non-multi-tenant fallback.
        return Path(base)
    return Path(base) / owner_hash


# ---------------------------------------------------------------------------
# Session owner verification helper (必改 3 & 9 — recommended implementation)
# ---------------------------------------------------------------------------

def assert_session_owner(db, session_id: str, owner_key: str) -> Optional[str]:
    """Verify that *session_id* belongs to *owner_key*.

    Returns ``None`` when the session exists AND is owned by *owner_key*;
    otherwise returns a **user-facing error string**. Crucially, both the
    "session does not exist" and "session exists but belongs to someone
    else" cases return the *same* "not found" message, so a caller cannot
    distinguish them — this prevents session-id / title enumeration across
    users (anti-enumeration, see plan §3).

    Args:
        db: a ``SessionDB`` instance.
        session_id: the target session id to check.
        owner_key: the current owner key (the caller has already resolved it
            via :func:`get_current_owner_key` in multi-tenant mode, or
            passed "" in single-user mode where this is a no-op).

    When *owner_key* is empty (single-user / non-multi-tenant path), the
    check is skipped and ``None`` is returned — preserving legacy behavior.
    """
    # Non-multi-tenant path: no owner partitioning, allow through.
    if not owner_key:
        return None
    if not session_id:
        return f"session not found: {session_id}"
    try:
        session = db.get_session(session_id)
    except Exception:
        # DB error -> treat as not found rather than leaking a different
        # signal. Log for operators.
        logger.debug("assert_session_owner DB read failed for %s", session_id, exc_info=True)
        return f"session not found: {session_id}"
    if not session:
        return f"session not found: {session_id}"
    # Compare against the persisted owner_key column (必改 1). Sessions
    # created before the column existed (or outside multi-tenant mode) have
    # a NULL/empty owner_key and are treated as unowned — i.e. visible to
    # nobody under multi-tenancy, which is the safe default.
    if (session.get("owner_key") or "") != owner_key:
        # Same message as the not-found branch: anti-enumeration.
        return f"session not found: {session_id}"
    return None


# ---------------------------------------------------------------------------
# Sandbox override builder for owner-scoped isolation (必改 5 & 6)
# ---------------------------------------------------------------------------

def build_owner_sandbox_overrides(owner_key: str) -> dict:
    """owner_key → 一份钉死的 docker 沙箱 override（供 register_task_env_overrides）。

    所有隔离/加固选项在这里集中设死，调用方不再散落配置。
    """
    sb = sandbox_config()
    root = owner_workspace_root(owner_key)
    # 审查 R3#2：owner workspace 目录必须先存在，否则 docker.py:597 的 isdir 检查
    # 不通过 → host_cwd 不挂到 /workspace → 退回 sandbox 临时目录（隔离失效）。
    root.mkdir(parents=True, exist_ok=True)
    return {
        "env_type": "docker",
        "docker_image": sb.get("image", ""),
        "host_cwd": str(root),
        "docker_mount_cwd_to_workspace": True,   # 没这个 host_cwd 不会挂到 /workspace
        "cwd": "/workspace",
        "network": False,                         # 默认禁网
        "docker_volumes": [],                     # 丢弃 operator 任意 volumes
        "container_persistent": True,             # 进程内复用
        "docker_persist_across_processes": False, # 关跨进程复用
        "mount_credentials": False,               # 全局凭证绝不进容器
        "mount_skills": True,                      # 公共业务 skills（RO）保留
        "mount_cache": False,                      # 全局 cache 含他人上传，关
        "container_cpu": int(sb.get("cpus", 2)),
        "container_memory": int(sb.get("memory_mb", 4096)),
    }
