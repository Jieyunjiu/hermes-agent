# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 权威开发指南

**架构、plugin/skill 体系、配置、已知陷阱、贡献准则的完整说明在 `AGENTS.md`（英文权威版）里，先读它。** 中文完整版见 `HUMAN_zh.md`（内容等价，便于人类阅读）。本文件只补两份指南没有、但每次开工都该记住的高价值内容，不重复它们。

## 两条不可破的设计铁律

任何改动都要先过这两关（review 一切代码的尺子）：

1. **每会话 prompt 缓存神圣不可破。** 绝不中途改动过去的上下文、切换工具集、或重建 system prompt（唯一例外是上下文压缩）。破缓存会成倍放大用户成本。改 system-prompt 状态的斜杠命令必须缓存感知：默认延迟失效，opt-in `--now` 才立即失效。
2. **内核是窄腰，能力长在边缘。** 每个 model tool 随每次 API 调用发送，新增核心工具门槛极高。新能力按「足迹阶梯」从高到低选：扩展现有代码 → CLI 命令 + skill → service-gated 工具 → plugin → MCP server → 新核心工具（最后手段）。

还要保护：严格的消息角色交替（绝不连续两条同角色消息、绝不循环中途注入合成 user 消息），以及 profile 安全（路径用 `get_hermes_home()` / `display_hermes_home()`，绝不硬编码 `~/.hermes`）。

## 常用命令

```bash
source .venv/bin/activate                              # 优先 .venv，回退 venv

# 测试：永远用 wrapper，不要直接调 pytest（wrapper 强制 CI 一致的 hermetic 环境）
scripts/run_tests.sh                                   # 全套
scripts/run_tests.sh tests/gateway/                    # 一个目录
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # 一个测试
scripts/run_tests.sh --no-isolate tests/foo/           # 关子进程隔离（调试更快）

# TUI（在 ui-tui/ 下）
cd ui-tui && npm run dev / build / typecheck / lint / test
```

## 当前任务：企业微信多租户改造

正在把 Hermes 从"个人助手"改成"单 gateway 进程服务多个企业微信用户"的内部助手，按统一 **owner_key**（`wecom:{corp_id}:{app_id}:{user_id}`）做默认数据隔离。

- **方案主文档**：`study/multi-tenant-wecom-rebuild-plan.md`（v7 九必改定稿，每条断言带源码行号）。
- **架构分析**：`study/hermes-agent-architecture-analysis.md`。
- **进度记录**：`study/process/*.md`——每次推进按"做了什么 / 已执行验证 / 仍未执行"记一笔；HUMAN_zh.md 末节有当前进度概览。

改动红线（在两条全局铁律之外）：

- **fail-closed**：多租户模式下取不到 owner_key 必须拒绝请求，绝不放行成无 owner 的全局会话。
- **不泄漏 session 存在性**：owner 校验失败返回 "not found" 而非 "forbidden"（防枚举）。推荐统一用 `assert_session_owner()`。
- **session_search 两条路径都要校验**：registry handler（`tools/session_search_tool.py`）+ `invoke_tool` 特殊分支（`agent/agent_runtime_helpers.py`，不经 registry）。
- **默认行为不变**：`security.multi_tenant.enabled` 默认 `false`，单用户体验不受影响。

## 协作约定

- 与用户沟通、注释、文档默认用**中文**（技术术语/变量名/路径保留英文）。
- 涉及修改代码/配置/文档时，**先给计划、等用户明确确认再动手**；只读侦察不需确认。
- 不主动执行 git 操作（commit/push/reset 等），需要时给命令 + 解释风险，由用户自己执行。
- 当前阶段 ruff / mypy / 全量 pytest 按用户要求暂时跳过，验证以目标逻辑的定向 pytest 为主；不要把"定向验证通过"写成"整体验收完成"。
