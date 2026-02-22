# PR Description (copy into your Pull Request)

## Summary
- **Queue deadlock fix:** Phase 1a and Phase 2 workers use `put_nowait` + per-worker `pending_discovery` and `_drain_pending_to_queue()` so no one blocks on `put()` when the queue is full.
- **Per-directory entry cap (I/O stall fix):** New `--max-entries-per-dir` (default 0 = no limit). When set (e.g. 50000), Phase 1a re-queues a directory after that many entries and processes other dirs, so a single huge directory cannot monopolize a worker for hours. Re-scanned dirs are deduplicated via a `discovered_dirs` set.

## For operators
If Phase 1a discovery stalls (queue stuck near full, `dirs_per_second` drops to near zero for a long time), set `--max-entries-per-dir 50000` or `EFSPURGE_MAX_ENTRIES_PER_DIR=50000` so very large directories are processed in chunks.

## Version
- Bump to 2.0.2.
