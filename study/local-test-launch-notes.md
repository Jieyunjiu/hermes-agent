# 本地测试启动笔记（改造版 Hermes）

> 记录本地（WSL）测试改造版 Hermes 时反复踩到、但从代码/README 里不直观的坑。
> 与个人官方 Hermes 隔离，统一用 `HERMES_HOME=~/.hermes-dev` 显式注入启动。

## 启动命令

```bash
source .venv/bin/activate                 # 改造版固定用项目内 .venv
git checkout self-native-sandbox           # 全原生沙箱方案在此分支（不在 self）
HERMES_HOME=~/.hermes-dev hermes gateway   # 用 dev 目录隔离，不碰 ~/.hermes
```

## 本分支 self-native-sandbox 配置增量（全原生数据处理沙箱化）

> 相对上一版（受限工具 + Option A）的差异。**企业微信接入完全不用改**（gateway/回调/owner_key 解析未碰）。

**这次的本质变化**：file 工具 **和** execute_code 现在都**在 owner docker 容器里执行**（上一版 file 是宿主进程内）；terminal 已在容器里；vision_analyze 进了工具集；skills 来源锁死为 operator 只读。工具集收敛为 13 个：
`read_file write_file patch search_files terminal execute_code skills_list skill_view vision_analyze memory session_search todo clarify`（不含 process/read_terminal/web/skill_manage）。

**沿用上一版、确认还在即可（已核对✅）**：
- `security.multi_tenant.enabled: true`、`workspace_root: ~/.hermes-dev/workspace`（可写）
- `security.multi_tenant.sandbox.{enabled: true, image: hermes-sandbox:latest, cpus, memory_mb, max_concurrent}`
- `default_reset_policy.mode: none`
- 依赖 `aiohttp` + `defusedxml`（见坑 1）
- 镜像 `hermes-sandbox:latest` 已 build（file/execute_code/terminal 三者现在都在它里面跑，数据库 pandas/pdfplumber/python-docx 等对三者都生效）

**这次新增/要注意的**：
1. **vision provider（当前是 `auto`，非本地模型）**：vision_analyze 现在在工具集里。要"本地视觉模型省成本+不外泄"，需把 `auxiliary.vision`（provider/base_url/model）指向**内网端点**。
   - 本地还没部署本地视觉模型时：Opus 4.8 多模态 + `agent.image_input_mode: auto` → 图片走**主模型原生看图**，图片任务正常；但模型若主动调 `vision_analyze` 工具，`provider=auto` 可能路由到外部（与不外泄冲突）且无 api_key 多半失败。**部署本地视觉模型后务必改指向内网**。
2. **skills 只认 `HERMES_HOME/skills`**：多租户下 `skills.external_dirs` 与 plugin skill **被忽略/拒绝**。公司技能放 `~/.hermes-dev/skills`（已在✅）。
3. execute_code 沙箱内回调工具用的是 **file-based RPC over docker exec**，禁网下也能工作，无需挂 socket/额外配置。

**代码里已钉死、无需配置**：`--network=none`、`docker_persist_across_processes=false`（退出即回收）、`mount_credentials=false`、`mount_cache=false`、`mount_skills=true`（`build_owner_sandbox_overrides`）。

**实测重点**：上传→execute_code 跑 pdfplumber→write_file 出 word，三者同容器同 `/workspace` 接力；A/B 双 owner 互相看不到文件；容器内 `pip install` 外网失败（禁网生效）。

## 坑 1：企业微信依赖跨两个 extra，只装 `[wecom]` 不够

**现象**：启动报

```
Platform 'WeCom (Enterprise WeChat)' requirements not met (pip install 'hermes-agent[wecom]')
Platform 'wecom' is registered but adapter creation failed
```

**根因**：企业微信回调适配器的依赖检查要 **三个库同时满足**
（`plugins/platforms/wecom/callback_adapter.py` 里 `check_wecom_callback_requirements()`
= `AIOHTTP_AVAILABLE and HTTPX_AVAILABLE and DEFUSEDXML_AVAILABLE`）：

| 依赖 | 由哪个 extra 提供 | 用途 |
|---|---|---|
| `httpx==0.28.1` | 核心依赖（默认就有） | 主动调用企业微信 API |
| `aiohttp==3.13.4` | **`[messaging]`** extra | 回调 HTTP server |
| `defusedxml==0.7.1` | **`[wecom]`** extra | 安全解析未鉴权回调 XML（防 XXE / XML 炸弹）|

`[wecom]` extra **故意只声明 defusedxml**（`pyproject.toml:165` 注释：
"aiohttp/httpx are already in [messaging]"），它假设 aiohttp 由 `[messaging]` 提供。
所以**只装 `[wecom]` 会缺 aiohttp**，报错信息又只提示装 `[wecom]`，很有迷惑性。

**为什么重建 venv 后才出现**：旧 venx 装过 `[messaging]`（带 aiohttp），
干净重建的 `.venv` 没装，才暴露出来。这与多租户/沙箱改造无关。

**修复（二选一）**：

```bash
# 声明式，一次装齐企业微信真正需要的两个 extra（推荐）
pip install -e '.[messaging,wecom]'

# 或最小改动，只补缺的（注意钉版本，aiohttp 3.13.4 修了一串 CVE）
pip install 'aiohttp==3.13.4' 'defusedxml==0.7.1'
```

**验证**（应输出 `all deps ok`）：

```bash
python -c "import aiohttp, httpx, defusedxml.ElementTree; print('all deps ok')"
```

## 坑 2：多租户沙箱 `workspace_root` 默认 `/data/workspaces`，WSL 下建不了

打开沙箱执行时，`workspace_root` 默认 `/data/workspaces`，代码会 `mkdir(parents=True)`
去建 `<root>/<owner_hash>`——WSL 里 `/data` 非 root 建不了，沙箱直接起不来。
**本地必须**在 `~/.hermes-dev/config.yaml` 把它改到有写权限的路径，例如：

```yaml
security:
  multi_tenant:
    workspace_root: /home/looking/.hermes-dev/workspaces
```

（沙箱完整配置见 `study/multi-tenant-sandbox-exec-design.md` §8 与本轮启动方案。）
