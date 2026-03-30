# Anti-Suicide Skill for OpenClaw

OpenClaw 是一个 always-on 的个人助手。一次错误的配置写入就可能导致 gateway 无法启动、频道全部断连、或者 agent 行为被静默篡改。

**anti-suicide** 是一套安全护栏，在每次自我修改前自动备份，修改后持续监控健康状态，一旦检测到服务退化就立即回滚并重启。

---

## 工作原理

```
Agent 请求写入配置文件
        │
        ▼
┌─────────────────────┐
│  Plugin (index.ts)  │  ← OpenClaw 的 before_tool_call 钩子
│  拦截 Write/Edit/   │
│  MultiEdit/Bash     │
└────────┬────────────┘
         │ 检测到目标是关键文件
         ▼
┌─────────────────────────────────────────────┐
│  Supervisor (supervisor.py)                 │
│                                             │
│  1. snapshot  ── 健康基线 + 文件备份（同步） │
│  2. verify    ── 后台持续监控 60s（异步）    │
│     └─ 如果健康退化 → 自动回滚 + 重启 gateway│
└─────────────────────────────────────────────┘
         │
         ▼
  工具调用正常执行（不阻塞）
```

**核心设计**：plugin 不阻止修改，而是在修改前拍快照，修改后启动后台监控。如果出问题，supervisor 自动恢复到修改前的状态。

---

## 防护范围

### OpenClaw 核心配置

| 文件 | 风险 |
|------|------|
| `~/.openclaw/openclaw.json` | Gateway 无法启动，所有频道断连 |
| `~/.openclaw/workspace/AGENTS.md` | Agent 身份 / 能力被静默篡改 |
| `~/.openclaw/workspace/SOUL.md` | 人格和核心行为被覆盖 |
| `~/.openclaw/workspace/TOOLS.md` | 工具列表损坏 |
| `~/.openclaw/.env` | API key 或频道 token 丢失 |
| `docker-compose.yml` | 容器无法启动 |

### 系统级网络 / 防火墙 / 代理配置

| 文件 | 平台 | 风险 |
|------|------|------|
| `hosts` | 全平台 | DNS 解析被劫持，服务无法连接上游 |
| `resolv.conf` | Linux/macOS | DNS 解析器配置损坏 |
| `.gitconfig` / `.curlrc` | 全平台 | HTTP 代理设置被篡改 |
| `.bashrc` / `.profile` | 全平台 | Shell 代理环境变量被修改 |
| `rules.v4` / `ufw.conf` / `firewalld.conf` | Linux | 防火墙规则变更导致出站流量被阻断 |
| `com.apple.alf.plist` | macOS | 系统防火墙配置 |

> Windows 防火墙规则存储在注册表中，无法通过文件监控捕获。为此 supervisor 增加了 `probe_outbound()` TCP 出站探测（默认 `8.8.8.8:443`），覆盖防火墙变更导致的连接中断。

---

## 健康探针

supervisor 使用 4 个维度的探针判断服务是否健康：

| 探针 | 检测目标 | 说明 |
|------|----------|------|
| `probe_doctor` | `openclaw doctor` CLI | 整体健康自检 |
| `probe_gateway_port` | TCP `127.0.0.1:18789` | Gateway 进程是否存活 |
| `probe_channels` | `/api/v1/channels/status` | 各频道连接状态 |
| `probe_outbound` | TCP `8.8.8.8:443` | 出站网络连通性（防火墙/路由） |

**任一探针失败** → 判定为服务退化 → 触发自动回滚。

---

## Supervisor 命令

```bash
# 快照：备份文件 + 记录健康基线
SESSION=$(python supervisor.py snapshot --files <file1> [file2 ...])

# 验证：修改后持续监控，退化则自动回滚
python supervisor.py verify --session $SESSION --timeout 60 --interval 5

# 监控模式：持续轮询文件变化（覆盖手工编辑场景）
python supervisor.py watch --files <file1> --system --interval 3

# 手动回滚
python supervisor.py rollback --session $SESSION

# JSON 预校验
python supervisor.py validate-json --content '{"key": "value"}'
```

### `watch` — 手工修改监控

plugin 只能拦截 AI 工具调用。当人类直接用文本编辑器修改配置文件时，需要 `watch` 命令：

```bash
# 监控 OpenClaw 配置 + 自动发现的系统配置文件
python supervisor.py watch \
  --files ~/.openclaw/openclaw.json \
  --system \
  --interval 3 \
  --verify-timeout 60 \
  --verify-interval 5
```

工作流程：
1. 启动时备份所有被监控文件（"安全状态"）
2. 每 `--interval` 秒通过 MD5 哈希检测文件变化
3. 检测到变化后，监控健康状态 `--verify-timeout` 秒
4. 如果健康退化：回滚到上一个安全状态
5. 如果健康保持：将当前状态提升为新的安全状态，继续监控

`--system` 标志会自动发现当前平台的网络 / 防火墙 / 代理配置文件并加入监控列表。

---

## 项目结构

```
anti-suicide-skill/
├── plugin/                         # TypeScript 插件（编译为 dist/index.js）
│   ├── index.ts                    # 主入口：before_tool_call 钩子
│   ├── package.json                # 插件元数据
│   ├── tsconfig.json               # TypeScript 编译配置
│   └── openclaw.plugin.json        # 插件注册清单
│
├── scripts/                        # Python 监控脚本
│   ├── supervisor.py               # 核心：健康探测 / 备份 / 回滚 / 监控
│   └── test_supervisor.py          # 端到端测试
│
├── references/
│   └── critical_paths.md           # 关键文件清单及风险说明
│
├── SKILL.md                        # Agent 可读的安全修改协议
├── INSTALL.md                      # 安装步骤
└── README.md                       # ← 你正在看的文件
```

---

## 安装

详见 [INSTALL.md](./INSTALL.md)。概要：

```bash
# 1. 安装 Skill
cp -r {SKILL.md,scripts,references} ~/.openclaw/workspace/skills/anti-suicide/

# 2. 安装 Plugin
openclaw plugins install ./plugin

# 3. 在 openclaw.json 注册
# plugins.entries 下添加 "anti-suicide": { "enabled": true, "config": {} }

# 4. 重启 Gateway
openclaw gateway restart
```

### 插件配置项

在 `openclaw.json` 的 `plugins.entries.anti-suicide.config` 中可选配置：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `supervisorPy` | string | `~/.openclaw/workspace/skills/anti-suicide/scripts/supervisor.py` | supervisor.py 的绝对路径 |
| `verifyTimeout` | number | `60` | 修改后监控持续时间（秒） |
| `verifyPreDelay` | number | `5` | 首次健康检查前的等待时间（秒） |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTI_SUICIDE_CONNECTIVITY_HOST` | `8.8.8.8` | 出站连通性探测的目标地址 |

---

## 测试

```bash
python scripts/test_supervisor.py
```

测试覆盖：
- `validate-json` — 合法 / 非法 JSON 输入
- `snapshot` — 文件备份 + 基线快照
- `verify` 正常路径 — 健康状态保持，退出码 0
- `verify` 回滚路径 — 健康退化后自动恢复原始文件，退出码 2

---

## 设计决策

**为什么不阻止修改？**
阻止修改会产生误报噪音，用户会很快关闭这个功能。更好的策略是允许修改但保证能恢复——类似数据库的事务日志。

**为什么 snapshot 是同步的、verify 是异步的？**
snapshot 必须在文件被修改前完成（否则备份的就是修改后的内容）。verify 如果也是同步的，每次修改都会卡 60 秒，不可接受。

**为什么 watch 用 MD5 轮询而不是 inotify / FSEvents？**
跨平台兼容（Windows + Linux + macOS）且零外部依赖。`watchdog` 库更高效，但需要 `pip install`，对于一个安全工具来说额外依赖越少越好。

**为什么需要 outbound 探测？**
Windows 防火墙规则在注册表中，不是文件，无法通过 watch 捕获。Linux 的 `iptables` 规则文件虽然可以监控，但通过 `iptables` CLI 直接修改规则不会写文件。TCP 探测是唯一可靠的方式来检测出站连通性被阻断。

---

## License

MIT
