# 未来阶段设计备忘：多租户 cron + owner-keyed home channel

> 状态：**设想，未实现**。当前一期不做（cronjob 不在 13 工具集）。等要开定时任务时，这两半必须**同步**改。
> 日期：2026-07-02

## 背景
- **home channel** = Hermes 主动找用户时（cron 结果、跨平台消息、网关生命周期通知）默认投递到哪个聊天。平时是被动回复当前聊天；主动消息没有"你刚发的话"可回，所以需要一个默认目的地。
- 现状：home channel 是**单个全局值**（env `WECOM_HOME_CHANNEL` / `config.home_channel`，见 `gateway/config.py`、`cron/scheduler.py:223`）；`/sethome` 写全局（`gateway/slash_commands.py:2034`）。触发提示在 `gateway/run.py:9540`（空 history + env 未设）。
- cronjob 现在被排除在多租户 13 工具集之外，正因为它是全局的、未 owner 收敛。

## 问题（为什么现在不能简单设个全局 home）
若把全局 home 设成管理员、之后又开 cron：用户 A 的定时任务结果会投到**管理员**，且管理员会收到**所有用户**的结果 → 错路由 + 跨租户泄漏。所以"设全局 admin"是隐患，当前**只压提示、保持 home 未设**即可（反正没主动投递）。

## 未来方案（cron 开启时，两半同步做）
**这是"多租户 cron"完整的一期，包含来源端 + 投递端两半：**

1. **owner-keyed home channel（投递端）**
   - 用 `owner_key → 该用户自己的 chat_id` 映射，替换单一全局值。存储按 owner 哈希（同 memory/session 隔离方式）。
   - 多数情况**不需要用户手动 /sethome**：企业微信 1:1 私聊，用户跟机器人对话的聊天就是他的 home，首条消息时**静默自动记录**即可。先例：`gateway/platforms/yuanbao.py:5087` 的 auto-sethome（第一个来消息的用户成 owner）。
   - 投递时按主动消息所属 owner_key 查其 home；**查不到 fail-closed 拒发/丢弃 + 记日志，绝不回退全局/管理员**（堵住泄漏点）。
   - `/sethome` 语义改成"设我自己的 home"，或干脆自动派生、不暴露给终端用户。

2. **owner 收敛的 cron（来源端）**
   - 每个定时任务打上 `owner_key` 标签：只属于该 owner、只用他的工具面/工作区（owner 容器）跑、结果只发他的 home。
   - fail-closed：任务缺 owner_key 不执行。

**完成标志**：A 设的定时任务只有 A 收到结果、跑在 A 的沙箱里、A 之间互不可见；然后才可把 cronjob 加进多租户工具集。

## 关联
- 当前沙箱方案：`study/multi-tenant-full-native-sandbox-design.md`（V1）
- owner 隔离地基：`study/multi-tenant-wecom-rebuild-plan.md`（九必改）
