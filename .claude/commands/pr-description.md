---
description: Generate and update PR description based on git changes
---

Please analyze the changes in my current branch compared to main and:

1. Get the PR diff and commit history:
   - Use `gh pr diff` to get the actual changes in the PR (this matches what GitHub UI shows)
   - Use `gh pr view --json title,number` to get PR info
   - Use `git log --first-parent main..HEAD --no-merges --oneline` to see commits
   - DO NOT use `git diff main...HEAD` as it includes merged changes from main
2. Generate a professional PR description with:
   - Summary (2-3 sentences about what changed and why)
   - Changes section (bulleted list with bold headers describing each major change)
   - Technical Details section (explain what the implementation enables/improves)
3. Use `gh pr edit --body` to update the current PR description automatically

Keep the description concise, focused on business value and technical implementation. Do not add unnecessary superlatives or praise.

The key addition is using gh pr diff which shows only the changes YOU introduced in the PR, not changes that were merged from main into your branch.
