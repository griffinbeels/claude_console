# Parallel work

## Parallel work

This repo is small and usually edited alone, so committing straight to `main` is
fine. If it ever needs a worktree, the rules are task_tracker's: branch from
local `main` HEAD, worktree gets its own `.venv`, run the suite from the
worktree root with a relative path.

**The cross-repo case is the one to watch.** A change here plus a change in a
consumer are two commits in two repos with no shared history, so nothing makes
them atomic. Land the consumer-compatible change here first, then the consumer
— a shared module that briefly leads its consumers is harmless, one that briefly
lags them breaks every window on the machine at once.
