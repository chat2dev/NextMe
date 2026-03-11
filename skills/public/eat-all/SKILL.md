---
name: eat-all
description: Analyzes any external resource and recommends the best way to integrate it into your Claude workflow. Give it a GitHub repo URL, an article link, a code snippet, a technical doc, or someone else's skill directory — it reads it deeply, then tells you exactly what form to put it in: a new Skill, an existing Skill extension, a project CLAUDE.md rule, a global CLAUDE.md preference, or a memory note. Use this skill when the user says "eat this", "eat-all", "digest this", "吃掉这个", "分析这个", pastes a URL or path and asks how to "add it", "learn from it", "absorb it", or "turn it into a skill". Also trigger when user drops a link/code and asks what to do with it from a knowledge-integration perspective.
---

# Eat All — Knowledge Digestion Advisor

You receive something external — a URL, a GitHub repo, a doc, a code snippet, or someone else's skill directory — and your job is to **deeply read it** then recommend the **most fitting digestion form** for integrating it into the user's Claude workflow.

## The digestion forms

| Form | Best for |
|------|----------|
| **New Skill** | A repeatable multi-step workflow; has a natural trigger phrase; involves specific tools or commands; something the user will invoke again and again |
| **Extend existing Skill** | The content augments or fills a gap in a workflow that already has a skill |
| **Project `CLAUDE.md`** | Project-specific conventions, architecture decisions, tech stack rules, file layout, team norms — context every session with this project should know |
| **Global `~/.claude/CLAUDE.md`** | Personal cross-project style preferences, universal habits about how the user wants Claude to behave |
| **Memory note** | A fact or preference that's personal and session-relevant but doesn't warrant a workflow or rule |
| **Not worth adding** | Knowledge Claude already has well-covered; content too narrow for a single use; or better bookmarked than encoded |

## Steps

### 1. Fetch and read the content

- **URL / article**: use WebFetch to retrieve and read the full page
- **GitHub repo URL**: fetch the README first, then key source files, existing scripts, and any workflow/config files (`.github/workflows`, `Makefile`, etc.)
- **File path or skill directory**: read SKILL.md and all related files
- **Inline code or text**: analyze directly — no fetching needed

If the content is large, focus on the parts that reveal **what workflow it enables** or **what knowledge it encodes**, not every implementation detail.

### 2. Analyze

Extract the core signal:

- **Domain**: what area does this cover? (CLI tooling, frontend, data, infrastructure, writing, …)
- **Type**: workflow / API reference / convention / concept / tool configuration
- **Repeatability**: would the user invoke this pattern more than once?
- **Scope**: applies to one project, or universally?
- **Trigger**: if it's a workflow — what would a user naturally say to start it?
- **Overlap**: does anything in `~/.claude/skills/` or the project already cover this?

### 3. Recommend

Present a single primary recommendation in this structure:

```
## Digestion Recommendation: [Form]

**Why this form**: [2–3 sentences — the key reason this form fits better than alternatives]

**What to extract**: [The specific knowledge, workflow steps, or rule to capture — be concrete]

**Draft**:
[A ready-to-use or near-ready artifact:
 - For a Skill: the full SKILL.md content
 - For CLAUDE.md: the specific lines/section to add
 - For a memory note: the note text]

**Alternatives considered**:
- [Other form]: [Why it's less suitable in one sentence]
```

If two forms are genuinely equally good, present both ranked — but don't hedge. Pick one.

### 4. Offer to implement

After the recommendation, ask: **"Should I go ahead and create this?"**

If yes, implement:
- **New Skill** → create `~/.claude/skills/<trigger>/SKILL.md` (or under the project if project-specific)
- **Extend existing Skill** → edit the relevant SKILL.md
- **Project CLAUDE.md** → append the rule to `CLAUDE.md` in the current working directory (create if missing)
- **Global CLAUDE.md** → append to `~/.claude/CLAUDE.md` (create if missing)
- **Memory note** → write to `~/.claude/memory/` or advise using `/remember`

## Decision heuristics

- **Clear trigger phrase + repeatable steps → Skill**. If you can't phrase a natural "When user says X" trigger, it's probably not a Skill.
- **Workflow vs context**: Skills run procedures. CLAUDE.md stores context. Don't put lookup tables or project conventions in a Skill.
- **Project vs global**: Ask yourself — would this rule make sense in a completely different codebase? If yes, global. If no, project.
- **Don't over-encode**: A long reference doc is usually better fetched on demand than stuffed into a rule. Prefer pointing to the source over copying it verbatim.
- **Skills are for humans to invoke**: if the user would never type the trigger, it shouldn't be a Skill.
