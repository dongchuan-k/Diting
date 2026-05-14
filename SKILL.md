---
name: diting
slug: diting
version: 1.5.0
description: 谛听 — 微信聊天AI总结助手，自动提取微信群聊和私聊消息并生成结构化日报
agent_created: true
created_at: 2026-05-14
---

# 谛听（DiTing）— 微信情报日报助手

> ⚠️ 仅支持 macOS（Apple Silicon M系列芯片）
> ⚠️ 涉及微信聊天记录，注意隐私保护。详细说明见 README 隐私章节。

## 能力

谛听可以：
1. 自动提取微信群聊和私聊消息
2. 用 AI 生成结构化日报（热点、决策、待办、商机、风险）
3. 支持定期自动总结（每日/按需）

## 触发方式

直接说中文即可，例如：

- "帮我总结一下项目群今天的消息"
- "帮我看看张三最近跟我聊了什么"
- "帮我把今天所有群的聊天记录总结一下"
- "帮我搜索聊天记录里关于报价的内容"
- "帮我导出项目群最近的消息"
- "帮我看一下昨天我的群聊聊了什么"

## 内部命令参考

| 用户意图 | 实际执行命令 |
|---------|------------|
| 总结指定群聊 | `diting summarize -c "群名" --today` |
| 总结所有配置群 | `diting summarize --all --today` |
| 总结私聊 | `diting summarize -c "联系人" --type contact --today` |
| 导出消息 | `diting export -c "群名" -n 200` |
| 搜索消息 | `diting search "关键词"` |
| 昨日总结 | `diting summarize --all --yesterday` |
| 系统自检 | `diting doctor` |
| 刷新密钥 | `diting keys refresh` |

## 出错时

命令执行失败时，先运行：
- `diting doctor` 检查环境
- 确认 `diting` 命令是否可用（运行 install.sh 后需重新加载终端）

## 隐私提醒

- 聊天记录仅在本地处理
- 如果启用了 AI 总结，消息会发送到 DeepSeek / OpenAI 的 API
- 可在 config.yaml 中开启脱敏（privacy.redact_before_ai）
- API Key 建议通过环境变量设置，不要写在配置文件中
