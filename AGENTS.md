# Working in this repository

Before answering questions about the current code or starting a change, run `git status --short --branch` and `git fetch origin`. Use `origin/main` to check recent shared changes, then read the current files relevant to the request. If the fetch fails, say that the remote state could not be verified.

When working from an existing checkout, compare it to the current remote with `git log --left-right --oneline HEAD...origin/main`. Preserve local edits. If local work prevents a clean update, use a separate worktree based on the current remote branch. A clean local branch that is only behind can be updated with `git merge --ff-only origin/main`.

Before publishing, fetch again. If the target branch has advanced, integrate the intervening changes and rerun the affected checks before publishing. Use a fast-forward update on shared branches so concurrent work is preserved. Verify the published commit on the remote.
