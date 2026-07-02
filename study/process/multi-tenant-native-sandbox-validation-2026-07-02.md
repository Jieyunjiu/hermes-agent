# 多租户原生数据处理沙箱化 — 真实环境验证记录（2026-07-02）

> 分支 `self-native-sandbox`（从 `self` 分出）。企业微信 dev 环境（WSL，`HERMES_HOME=~/.hermes-dev`）实测。
> 设计：`study/multi-tenant-full-native-sandbox-design.md`（V1）。计划：`study/execute-plans/2026-07-01-multi-tenant-native-sandbox-plan.md`。

## 做了什么

- 完成并逐任务审查 T1–T6（工具集13 / file 下沉容器 / execute_code 进容器 / vision 收敛 / skills 锁死 / 能力清单极简英文），最终整分支审查（opus）1 处 Important 已修，清理旧测试。
- 真实测试暴露并修复的收尾项：
  - **T8**：出站附件把容器 `/workspace/...` 映射回宿主 owner_root（否则报告做出来发不回）。
  - **T9**：能力清单列预装库（减少模型瞎试 pymupdf/pip install）。
  - **T10**：owner_key 在两条旁路的传播缺口——根因2 投递发生在 `_clear_session_env` 之后（投递前 `scoped_owner_key` 重绑定）；根因1 后台/重注入合成回合丢 owner_key（spawn 时捕获、重注入恢复、缺失 fail-closed）。
  - docstring 乱码修复（子代理写成 `\uXXXX` 转义）。
  - 缺能力快速失败：能力清单加"缺库不重试、告知用户联系 IT 管理员"。
- 部署配置（`~/.hermes-dev/config.yaml`，非代码）：
  - `security.multi_tenant.enabled/sandbox.{enabled,image,cpus,memory_mb,max_concurrent}`、`workspace_root`、`default_reset_policy.mode=none`。
  - 斜杠命令权限 `platforms.wecom.extra`：`allow_admin_from=[HanYuWen]`（admin 全命令）+ `user_allowed_commands`（普通用户 12 个）。
  - `tool_loop_guardrails.hard_stop_enabled` 改 `true`（重复失败到阈值强制停，防烧 token）。

## 已执行验证（真实企业微信，PASS）

- **命令分级**：管理员 `HanYuWen` `/whoami` = admin / all available；普通账号 `/whoami` = 仅受限集 + help/whoami，`/restart` 被拒。✅
- **PDF→Word 端到端**：企业微信发 PDF → agent 在 owner 容器用 pdfplumber/python-docx 生成 Word → **报告作为附件成功发回用户**；memory 不再崩（OwnerKeyMissing 已修）。✅
- **禁网隔离**：`docker run --rm --network=none hermes-sandbox:latest pip install cowsay` 失败、去掉 `--network=none` 成功（证明是禁网标志在挡）；容器内 curl 外网失败。✅
- 定向测试：各任务定向 pytest 全绿；T10 复现测试 3/3；能力清单 3/3。

## 仍未执行 / 待办

- T7 剩余项未逐条系统跑：A/B 双 owner 并发文件互不可见（逻辑已多处验证，未做正式并发压测）；三工具共用同一容器的接力（PDF 任务已隐含验证）。
- **vision provider 仍是 `auto`**（非 on-prem 本地视觉模型）：图片走主模型原生看图正常；待部署本地视觉模型后把 `auxiliary.vision` 指向内网。
- 已知遗留旁路（非阻塞，账本 `.superpowers/sdd/progress.md` 记着）：`/background` owner_key 缺口、`extract_local_files` 裸路径投递、kanban/cron 投递 owner_key、vision 内部调用（cache/vision、/tmp）可能被守卫误伤。
- 未来阶段：多租户 cron + owner-keyed home channel（`study/future-work-multitenant-cron-home-channel.md`）。
- 分支未合并、未 push（等用户决定 merge/PR）。
