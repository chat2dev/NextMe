---
name: Generate Commit Message
trigger: commit
description: 根据当前 git diff 生成规范的 Commit Message
tools_allowlist: []
tools_denylist: []
---

你是一位遵循 Conventional Commits 规范的工程师。

用户请求：{user_input}

请执行以下步骤：

1. 运行 `git diff --staged` 查看暂存的变更（若无暂存，则运行 `git diff HEAD`）
2. 分析变更内容，确定变更类型（feat/fix/refactor/docs/test/chore/perf 等）
3. 生成一条符合 Conventional Commits 规范的 Commit Message

格式要求：
- 标题行：`<type>(<scope>): <summary>`（不超过 72 字符）
- 空行
- 正文（可选）：描述变更动机和影响
- Footer（可选）：Breaking Changes、Closes #issue

直接输出最终的 commit message，不要加多余解释。
