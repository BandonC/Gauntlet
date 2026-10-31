Follow "Unattended runs" in CLAUDE.md.

Each iteration:
1. `git status`. If the previous iteration left uncommitted work, finish it or
   revert it before starting anything new.
2. `git checkout main`. Pick the first unchecked roadmap item in CLAUDE.md not
   tagged `[human]`. Create `claude/<short-item-name>` from main.
3. Implement it with tests. Run `ruff format arena tests`, then
   `DATABASE_URL=postgresql://arena:arena@localhost:5433/arena_test scripts/check.sh`.
   Always use scripts/check.sh, and never pipe test output through tail or head
   without `set -o pipefail`.
   Never use `git commit --no-verify`; if the hook blocks a commit, fix the cause.
4. When everything passes: tick the checkbox and commit on the branch. Then STOP
   the loop (don't schedule another iteration) and post: the branch name, a short
   summary, how it was tested, and "Reply 'approved' to merge". Never merge or
   push without my explicit approval in this conversation.
5. Also stop the loop, explaining what's blocked, if the next item is tagged
   `[human]` or needs a decision the roadmap doesn't settle, or if tests still fail
   after two fix attempts.

When I approve: `git checkout main && git merge --no-ff <branch>`, run `pytest`
on main, delete the branch, then confirm. Don't push. If I give feedback instead,
fix it on the branch, re-run the checks, and ask for approval again.
