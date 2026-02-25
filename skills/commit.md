---
name: Generate Commit Message
trigger: commit
description: Generate a Conventional Commits message from the current git diff
tools_allowlist: []
tools_denylist: []
---

You are an engineer who follows the Conventional Commits specification.

User request: {user_input}

Follow these steps:

1. Run `git diff --staged` to inspect staged changes (if nothing is staged, run `git diff HEAD` instead)
2. Analyse the changes and determine the commit type (feat / fix / refactor / docs / test / chore / perf / etc.)
3. Generate a single commit message conforming to Conventional Commits

Format requirements:
- Subject line: `<type>(<scope>): <summary>` (≤ 72 characters)
- Blank line
- Body (optional): describe motivation and impact
- Footer (optional): Breaking Changes, Closes #issue

Output the final commit message directly — no extra explanation.
