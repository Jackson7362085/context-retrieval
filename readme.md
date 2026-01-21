# Context Retrieval for Claude Code

> **让 Claude Code 具备“可持久化的工程记忆”**
> 将成功的对话经验自动归档为结构化知识，并在未来对话中主动复用。

---

## ✨ 项目简介

**Context Retrieval** 是一套为 **Claude Code** 设计的上下文记忆系统，由两部分组成：

1. **save_summary.py** —— 对话归档 Hook 插件
2. **claude-context-mcp** —— 上下文读取 MCP 服务器

它解决了 AI 编程助手的一个核心痛点：

> 👉 **对话结束后，成功经验会被“遗忘”**

本项目会在你明确确认“这次问题解决了”时，
将 **真正有效的解决路径** 持久化为结构化知识，并在后续对话中自动提供参考。

---

## 🧩 核心组件

### 1️⃣ save_summary.py — 对话归档器（Hook 插件）

作为 **Claude Code 的 `UserPromptSubmit` Hook**，监听并处理 `/save-summary` 命令。

#### 核心功能

* 拦截 `/save-summary` 命令
* 从 Claude Code 的 **transcript（对话日志）** 中读取历史对话（最近约 8000 行）
* 智能提取：

  * 用户目标
  * 关键对话
  * 工具调用与输出
  * **最终成功的解决路径**
* 通过 **Anthropic API** 调用 LLM（如 `claude-opus-4-5-thinking`）生成结构化归档
* 将结果写入项目的 `.claude/context/` 目录

#### 生成的结构

```
.claude/context/
├── index.json
└── resolutions/
    ├── res-001.json
    ├── res-002.json
    └── ...
```

* **index.json**：项目级知识索引

  * 当前目标
  * 项目状态
  * 已验证事实
  * 下一步行动

* **res-xxx.json**：具体问题的解决方案

  * problem：问题描述
  * root_cause：根因分析
  * final_fix：最终修复步骤（可复现）
  * verification：验证方法
  * anti_patterns：已踩过的坑
  * problem_signature：稳定错误关键词（用于匹配）

#### 设计特点

* ✅ 只记录**最终成功路径**，忽略失败尝试
* ✅ 强调 **可复现性**（final_fix + verification）
* ✅ 显式记录 **反模式（anti_patterns）**
* ✅ 支持 **增量合并**，不会覆盖历史记录

---

### 2️⃣ claude-context-mcp — MCP 上下文服务器

一个 **Model Context Protocol (MCP)** 服务器，用于在后续对话中读取已归档的知识。

#### 提供的工具

* `read_context_index`
  → 读取 `.claude/context/index.json`

* `read_context_resolution(res_id)`
  → 读取指定的 `res-xxx.json`

#### 工作方式

* 通过 **stdio** 与 Claude Code 通信
* 自动使用当前工作目录作为项目路径
* 返回 **JSON 结构化上下文**，供 Claude 直接理解和引用

---

## 🔄 整体工作流程

```
第一轮对话：解决问题
│
│ Claude 修复了某个复杂问题（如 Docker、Redis、网络冲突等）
│
└── 用户输入：/save-summary
        │
        ▼
save_summary.py (Hook)
- 读取对话 transcript
- 调用 LLM 归档成功经验
- 写入 .claude/context/
        │
        ▼
第二轮对话：遇到类似问题
│
│ Claude 调用 read_context_index
│ Claude 发现相关 res-003
│ Claude 调用 read_context_resolution("res-003")
│
└── 基于历史成功经验给出建议，避免重复踩坑
```

---

## 🚀 项目价值

这个系统为 AI 编程助手带来了 **真正可用的“工程级记忆”**：

* ✅ **知识持久化**：对话经验 → 结构化知识
* ✅ **跨会话记忆**：新对话可读取历史解决方案
* ✅ **避免重复犯错**：显式记录反模式
* ✅ **可验证性**：每个方案都有验证步骤
* ✅ **智能检索**：通过 problem_signature 快速匹配
* ✅ **增量更新**：持续积累，不覆盖历史

---

## 📦 安装

### 直接从 GitHub 安装插件

```bash
/plugin install github:Jackson7362085/context-retrieval
```

安装完成后可用命令：

```text
/context-retrieval:save-summary
```

---

## 🧹 卸载

```bash
/plugin uninstall context-retrieval
```

> 注意：卸载插件不会删除 `.claude/context/` 中已生成的知识文件，如需清理请手动删除。

---

## 📝 使用建议

* 在你**确认问题已成功解决**后再运行 `/save-summary`
* 把它当作“提交一次可复现经验”的动作
* 不要频繁保存失败尝试

---

## 📄 License

MIT


