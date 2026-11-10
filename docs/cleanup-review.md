# Clean-up review

Ranked by value. Behaviour must stay unchanged; one PR per area.

1. **Duplicate `_is_int` / bool-excluding int checks** (done in this PR): two copies of
   `_is_int` plus three inline `isinstance(x, int) or isinstance(x, bool)` checks, now
   `arena.types.is_int`.
2. **Duplicate `_retry_after` in the two vendor adapters** (`providers/anthropic_provider.py`,
   `providers/openai_provider.py`): same header parsing over different exception types.
   Share a helper taking the response headers.
3. **`_child` in `bench.py` and `chaos.py`**: near-identical worker-subprocess entry points.
4. **`_winner` in `balance.py` and `games/werewolf.py`**: same win rule on different shapes;
   have `balance` reuse the engine's.
5. **Unused-looking code (vulture, all used by tests or entry points; no dead code found):**
   `EloTable`, `index_of`, `ScriptedPlayer`, `AccuracyVoteBot`, `load_recording`, `verify`.
6. **Naming:** `store/tournaments.py` and top-level `tournaments.py` share a name; consider
   renaming the pure one to `scheduling.py`.
