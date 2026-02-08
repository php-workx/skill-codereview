---
description: Run AI-powered multi-pass code review using the codereview skill.
---

## User Input

```text
$ARGUMENTS
```

Use the `codereview` skill for this request.

Interpret `$ARGUMENTS` as follows:
- If it's a number, treat as a PR number: review PR #N.
- If it includes `--base`, review all commits on current branch since that base.
- If it includes `--range`, review the specified commit range.
- If it includes `--spec`, load the spec file for requirements checking.
- If it's a path, review changes in that path.
- If empty, auto-detect: staged changes or HEAD~1.

Then execute the workflow exactly as defined by the `codereview` skill.
