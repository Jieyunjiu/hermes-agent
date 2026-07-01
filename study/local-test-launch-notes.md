# 本地测试启动笔记（改造版 Hermes）

> 记录本地（WSL）测试改造版 Hermes 时反复踩到、但从代码/README 里不直观的坑。
> 与个人官方 Hermes 隔离，统一用 `HERMES_HOME=~/.hermes-dev` 显式注入启动。

## 启动命令

```bash
source .venv/bin/activate                 # 改造版固定用项目内 .venv
HERMES_HOME=~/.hermes-dev hermes gateway   # 用 dev 目录隔离，不碰 ~/.hermes
```

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
