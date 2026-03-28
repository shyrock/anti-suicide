# 安装说明

两部分需要分别安装：**Skill**（给 agent 的指令）和 **Plugin**（自动触发的代码钩子）。

---

## 第一步：安装 Skill

```bash
mkdir -p ~/.openclaw/workspace/skills/anti-suicide

cp SKILL.md            ~/.openclaw/workspace/skills/anti-suicide/
cp -r scripts          ~/.openclaw/workspace/skills/anti-suicide/
cp -r references       ~/.openclaw/workspace/skills/anti-suicide/
```

验证：
```bash
ls ~/.openclaw/workspace/skills/anti-suicide/
# 应该看到: SKILL.md  scripts/  references/
```

---

## 第二步：安装 Plugin（自动触发的核心）

### 2a. 复制文件

```bash
mkdir -p ~/.openclaw/extensions/anti-suicide

cp plugin/index.ts              ~/.openclaw/extensions/anti-suicide/
cp plugin/package.json          ~/.openclaw/extensions/anti-suicide/
cp plugin/tsconfig.json         ~/.openclaw/extensions/anti-suicide/
cp plugin/openclaw.plugin.json  ~/.openclaw/extensions/anti-suicide/
```

### 2b. 安装并构建插件

```bash
openclaw plugins install ~/.openclaw/extensions/anti-suicide
```

`openclaw plugins install` 会使用 OpenClaw 内置的构建环境编译 `index.ts → dist/index.js`，SDK 依赖（`openclaw/plugin-sdk/plugin-entry`）由 OpenClaw 自身提供，**不需要手动 `npm install`**。

如果安装失败并提示 `missing openclaw.extensions`，检查 `plugin/package.json` 里是否有：
```json
"openclaw": {
  "extensions": ["./dist/index.js"]
}
```

### 2d. 在 openclaw.json 里注册

编辑 `~/.openclaw/openclaw.json`，在 `plugins.entries` 下添加：

```json
{
  "plugins": {
    "entries": {
      "anti-suicide": {
        "enabled": true,
        "config": {}
      }
    }
  }
}
```

如果 `openclaw.json` 里已有 `plugins` 字段，只加 `"anti-suicide"` 那一行，不要覆盖整个 plugins 块。

---

## 第三步：重启 Gateway

```bash
openclaw gateway restart
```

---

## 验证安装是否生效

```bash
# 1. 检查 plugin 是否被识别
openclaw plugins list
# 应该看到 anti-suicide 出现在列表中

# 2. 检查 skill 是否被发现
openclaw skills list
# 应该看到 anti-suicide

# 3. 检查整体健康状态
openclaw doctor
```

### 功能性验证

用一个**测试文件**（不要用真实配置）触发一次修改，观察 supervisor 是否运行：

```bash
# 在另一个终端监控日志
tail -f ~/.openclaw/logs/gateway.log

# 然后让 agent 执行：
# "帮我在 ~/.openclaw/test-anti-suicide.txt 里写入 hello"
# （这个路径在 OPENCLAW_HOME 下，会触发插件）

# 检查是否生成了 session 目录
ls /tmp/anti-suicide-*/
```

---

## 常见问题

**plugin 安装后 gateway 报错启动失败**
→ 检查 `openclaw.plugin.json` 的 JSON 格式是否正确

**before_tool_call 没有触发**
→ 这个 hook 在部分版本有 bug，运行 `openclaw update status` 确认版本，
  必要时升级：`openclaw update --channel stable`

**supervisor.py 找不到**
→ 默认路径是 `~/.openclaw/workspace/skills/anti-suicide/scripts/supervisor.py`，
  确认 skill 已正确安装在第一步
