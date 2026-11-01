Follow "Unattended runs" in CLAUDE.md.

Each iteration:
1. `git status`. If the previous iteration left uncommitted work, finish it or
   revert it before starting anything new.
2. `git checkout main`. Take the next unchecked item in the lowest-numbered
   unfinished phase of the CLAUDE.md roadmap, skipping nothing: if that item is
   tagged `[human]`, stop (see below). Create `claude/<short-item-name>` from main.
3. Implement it with tests. Run `ruff format arena tests`, then
   `DATABASE_URL=postgresql://arena:arena@localhost:5433/arena_test scripts/check.sh`.
   Always use scripts/check.sh, and never pipe test output through tail or head
   without `set -o pipefail`.
   Never use `git commit --no-verify`; if the hook blocks a commit, fix the cause.
4. When everything passes: tick the checkbox and commit on the branch. Then
   `git checkout main && git merge --no-ff <branch>`, run scripts/check.sh on main,
   and delete the branch. If the merge conflicts or checks fail on main, abort the
   merge, leave the branch, and stop. NEVER push.
5. Post two lines: what merged and what's next. Then continue to the next item.

Paid runs (real model calls) are pre-approved only when ALL of these hold:
- the step is listed in the roadmap with a budget;
- a test game ran first and passed its gate: no max_tokens stops, no forfeits,
  illegal moves under 5%, cost per game within 1.5x of your estimate;
- the step's --budget is at most its listed budget, and total paid spend this loop
  session stays under $25.
Otherwise stop and ask. After each tournament: write the report into CLAUDE.md and
the README, re-export docs/, and commit it as its own item.

Stop the loop (don't schedule another iteration) and report when:
- the next item is tagged `[human]`, or needs a decision the roadmap doesn't settle;
- any gate fails, a budget would be exceeded, or a result contradicts your own
  prediction by more than its 95% interval;
- checks still fail after two fix attempts;
- a new dependency is needed that the roadmap doesn't pre-approve;
- the roadmap is finished.

Every final message ends with a running summary: branches merged this session,
paid runs and their cost, total paid spend this session, and anything left for me.