# Concurrency Tuning Guide

This guide helps you tune `--max-concurrency-scanning`, `--max-concurrency-deletion`, and other parallelism parameters using the enhanced rate metrics and concurrency utilization data logged by AsyncEFSPurge.

**Note:** `--max-concurrency` is deprecated but still works. It sets both scanning and deletion to the same value. Use separate parameters for better control.

## Understanding the Metrics

### Concurrency Utilization Metrics

These metrics appear in every progress log (every 30 seconds):

```json
{
  "active_tasks": 850,
  "max_active_tasks": 950,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000,
  "max_concurrency": 1000,
  "available_concurrency_slots": 150,
  "concurrency_utilization_percent": 85.0
}
```

**What they mean:**
- `active_tasks`: Current number of file operation tasks (includes both running and waiting for semaphore)
- `max_active_tasks`: Peak concurrent tasks created during this run (may exceed max_concurrency if tasks queue)
- `max_concurrency_scanning`: Your configured limit for scanning (stat) operations
- `max_concurrency_deletion`: Your configured limit for deletion (remove) operations
- `max_concurrency`: Maximum of scanning and deletion limits (for backward compatibility)
- `available_concurrency_slots`: Estimated available slots (max_concurrency - active_tasks, can be negative if queued)
- `concurrency_utilization_percent`: Percentage of max_concurrency (may exceed 100% if tasks are queued)

**Important Note:** `active_tasks` includes tasks that are waiting for the semaphore, not just tasks currently running. So `active_tasks` can exceed `max_concurrency` when many tasks are queued. For tuning, focus on whether `max_active_tasks` consistently approaches or exceeds `max_concurrency` - this indicates you're creating tasks faster than they complete.

**Scanning vs Deletion:** Scanning and deletion now use separate semaphores, allowing you to tune them independently. Scanning (stat operations) can often handle higher concurrency than deletion (remove operations).

### Rate Metrics

```json
{
  "files_per_second_overall": 415.2,
  "files_per_second_instant": 450.0,
  "files_per_second_short": 420.0,
  "scanning_files_per_second": 415.2,
  "deletion_files_per_second": 380.0,
  "peak_files_per_second": 500.0
}
```

**What they mean:**
- `files_per_second_overall`: Average rate since start
- `files_per_second_instant`: Rate in last 10 seconds (most recent)
- `files_per_second_short`: Rate in last 60 seconds
- `scanning_files_per_second`: Rate during scanning phase only
- `deletion_files_per_second`: Rate during deletion phase only
- `peak_files_per_second`: Maximum rate achieved

## Tuning Strategy

### Step 1: Baseline Measurement

Run with default settings and observe metrics:

```bash
efspurge /mnt/efs --max-age-days 30 --dry-run --log-level INFO
```

Look for these patterns in progress logs:

### Step 2: Analyze Utilization

**Underutilized (< 50% utilization):**
```json
{
  "active_tasks": 400,
  "max_active_tasks": 450,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000,
  "concurrency_utilization_percent": 45.0,
  "files_per_second": 200.0
}
```

**Diagnosis:** You're only using 45% of available concurrency.

**Action:** Increase concurrency limits:
```bash
efspurge /mnt/efs --max-age-days 30 --max-concurrency-scanning 2000 --max-concurrency-deletion 1000
```

**Well-tuned (70-90% utilization):**
```json
{
  "active_tasks": 850,
  "max_active_tasks": 950,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000,
  "concurrency_utilization_percent": 85.0,
  "files_per_second": 415.0
}
```

**Diagnosis:** Good utilization, concurrency is well-matched to workload.

**Action:** Keep current settings or try slight increase if rates are still improving.

**Saturated (> 95% utilization):**
```json
{
  "active_tasks": 980,
  "max_active_tasks": 1000,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000,
  "concurrency_utilization_percent": 98.0,
  "files_per_second": 500.0
}
```

**Diagnosis:** Constantly hitting concurrency limit.

**Action:** 
- If `files_per_second` is still increasing → increase `--max-concurrency-scanning` and/or `--max-concurrency-deletion`
- If `files_per_second` has plateaued → you may be filesystem-limited, not concurrency-limited

### Step 3: Compare Peak vs Current Rates

**If peak >> current:**
```json
{
  "files_per_second": 300.0,
  "peak_files_per_second": 500.0,
  "concurrency_utilization_percent": 60.0
}
```

**Diagnosis:** You achieved higher rates before, but current rate is lower.

**Possible causes:**
- Filesystem throttling (EFS burst credits exhausted)
- Network congestion
- Different file sizes/types (smaller files = faster)

**Action:** Monitor over time. If peak was brief, it may have been during optimal conditions.

**If peak ≈ current:**
```json
{
  "files_per_second": 450.0,
  "peak_files_per_second": 480.0,
  "concurrency_utilization_percent": 90.0
}
```

**Diagnosis:** Consistent high performance.

**Action:** Settings are optimal. Consider if slight increase helps, but current is good.

### Step 4: Compare Instant vs Short-term Rates

**If instant >> short-term:**
```json
{
  "files_per_second_instant": 600.0,
  "files_per_second_short": 400.0
}
```

**Diagnosis:** Recent performance spike, but average is lower.

**Action:** Monitor - may be temporary. If consistent, you may be hitting filesystem limits intermittently.

**If instant << short-term:**
```json
{
  "files_per_second_instant": 300.0,
  "files_per_second_short": 500.0
}
```

**Diagnosis:** Recent slowdown.

**Possible causes:**
- Filesystem throttling
- Larger files being processed
- Network issues

**Action:** Check for filesystem/network issues. May need to reduce concurrency if throttling.

### Step 5: Compare Per-Phase Rates

**Scanning vs Deletion:**
```json
{
  "scanning_files_per_second": 500.0,
  "deletion_files_per_second": 300.0
}
```

**Diagnosis:** Deletion is slower than scanning.

**Why:** Deletion operations may be slower due to:
- Filesystem metadata updates
- Network latency for delete operations
- Different I/O patterns

**Action:** This is normal. You can now tune them separately:
- If scanning is fast but deletion is slow → increase `--max-concurrency-deletion`
- If scanning is slow → increase `--max-concurrency-scanning`
- If both are slow → increase both, but deletion may need lower limits

**Example:** For AWS EFS, you might use:
```bash
efspurge /mnt/efs --max-age-days 30 \
  --max-concurrency-scanning 2000 \
  --max-concurrency-deletion 1000
```

## Filesystem-Specific Recommendations

### AWS EFS

**Default:** `--max-concurrency-scanning=1000`, `--max-concurrency-deletion=1000`

**Tuning:**
- **Start:** Scanning 1000, Deletion 1000
- **If utilization < 50%:** Increase scanning to 2000-3000, deletion to 1000-2000
- **If utilization > 95%:** Increase scanning to 3000-5000, deletion to 2000-3000
- **If rates plateau:** You may be hitting EFS IOPS limits, not concurrency limits

**EFS Considerations:**
- High latency benefits from high concurrency
- Scanning can often handle higher concurrency than deletion
- Burst credits can cause rate variations (watch instant vs short-term rates)
- Throughput mode vs Provisioned mode affects limits

**Recommended:** `--max-concurrency-scanning=2000 --max-concurrency-deletion=1000`

### Local Disk / NFS

**Default:** `--max-concurrency-scanning=1000`, `--max-concurrency-deletion=1000`

**Tuning:**
- **Start:** Scanning 1000, Deletion 1000
- **If utilization < 50%:** Increase both to 1500-2000
- **If utilization > 95%:** Increase both to 2000-3000
- **If rates plateau:** Likely disk I/O limited, not concurrency limited

**Considerations:**
- Lower latency = less benefit from high concurrency
- Disk I/O may be the bottleneck, not network
- Both scanning and deletion can often use similar limits on local disk

**Recommended:** `--max-concurrency-scanning=1000 --max-concurrency-deletion=1000`

### Object Storage (S3, etc.)

**Default:** `--max-concurrency-scanning=2000`, `--max-concurrency-deletion=2000`

**Tuning:**
- **Start:** Scanning 2000, Deletion 2000
- **If utilization < 50%:** Increase scanning to 3000-5000, deletion to 2000-3000
- **If utilization > 95%:** Increase scanning to 5000-10000, deletion to 3000-5000

**Considerations:**
- Very high latency = high concurrency helps
- API rate limits may be the bottleneck
- Scanning typically needs higher concurrency than deletion

**Recommended:** `--max-concurrency-scanning=3000 --max-concurrency-deletion=2000`

## Common Scenarios

### Scenario 1: Low Utilization, Low Rate

```json
{
  "concurrency_utilization_percent": 30.0,
  "files_per_second": 100.0,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000
}
```

**Problem:** Underutilized AND slow.

**Possible causes:**
- Filesystem is slow (not concurrency-limited)
- Very large files (each file takes long time)
- Network issues

**Action:**
1. Check filesystem performance independently
2. Check file sizes
3. If filesystem is fine, increase concurrency anyway (may help overlap I/O)

### Scenario 2: High Utilization, Low Rate

```json
{
  "concurrency_utilization_percent": 98.0,
  "files_per_second": 50.0,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000
}
```

**Problem:** Fully utilized but still slow.

**Possible causes:**
- Filesystem is the bottleneck (not concurrency)
- Very slow filesystem (old hardware, network issues)
- Large files

**Action:**
1. Check filesystem performance
2. May need to reduce concurrency to avoid overwhelming filesystem
3. Check if reducing concurrency improves rate (counterintuitive but sometimes helps)

### Scenario 3: Low Utilization, High Rate

```json
{
  "concurrency_utilization_percent": 40.0,
  "files_per_second": 2000.0,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000
}
```

**Problem:** Very fast but underutilized.

**Diagnosis:** Filesystem is very fast, doesn't need high concurrency.

**Action:**
- This is fine! Low concurrency = less memory usage
- You could reduce concurrency limits to save memory
- Or keep them for future-proofing if filesystem gets slower

### Scenario 4: High Utilization, High Rate

```json
{
  "concurrency_utilization_percent": 95.0,
  "files_per_second": 2000.0,
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000
}
```

**Problem:** Fully utilized and fast.

**Diagnosis:** Well-tuned! But could potentially go faster.

**Action:**
- Try increasing `--max-concurrency-scanning` to 1500-2000
- Try increasing `--max-concurrency-deletion` to 1500-2000
- Monitor if rate increases
- If rate doesn't increase, you're filesystem-limited (current is optimal)

## Tuning Workflow

### 1. Initial Run (Baseline)

```bash
efspurge /mnt/efs --max-age-days 30 --dry-run --log-level INFO > baseline.log
```

**Check:**
- `concurrency_utilization_percent`
- `files_per_second_overall`
- `peak_files_per_second`

### 2. If Underutilized (< 70%)

```bash
# Increase concurrency limits
efspurge /mnt/efs --max-age-days 30 --dry-run \
  --max-concurrency-scanning 2000 \
  --max-concurrency-deletion 1000 > test2x.log
```

**Compare:**
- Did `files_per_second` increase?
- Did `concurrency_utilization_percent` increase?
- Did `peak_files_per_second` increase?

### 3. If Saturated (> 95%)

```bash
# Increase concurrency limits
efspurge /mnt/efs --max-age-days 30 --dry-run \
  --max-concurrency-scanning 3000 \
  --max-concurrency-deletion 2000 > test3x.log
```

**Compare:**
- Did `files_per_second` increase?
- Did `max_active_tasks` increase?
- Or did rate plateau (filesystem-limited)?

### 4. Find Optimal

**Optimal settings show:**
- `concurrency_utilization_percent`: 70-90%
- `files_per_second` increasing with concurrency (up to a point)
- `peak_files_per_second` close to current rate (consistent performance)

## Other Parameters to Tune

### `--max-concurrent-subdirs`

**Default:** 100

**When to reduce:**
- Deep directory trees causing memory issues
- `memory_mb` growing too high
- OOM kills despite low `--max-concurrency`

**When to increase:**
- Shallow directory trees
- Memory is fine
- Want faster directory scanning

**Tuning:** Monitor `memory_mb` and `dirs_scanned` rate.

### `--task-batch-size`

**Default:** 5000

**When to reduce:**
- Memory issues
- Very large directories

**When to increase:**
- Memory is fine
- Want to process more files at once

**Tuning:** Monitor `memory_mb` and memory back-pressure events.

## Example: Tuning Session

### Run 1: Default (1000 scanning, 1000 deletion)
```json
{
  "max_concurrency_scanning": 1000,
  "max_concurrency_deletion": 1000,
  "max_active_tasks": 450,
  "concurrency_utilization_percent": 45.0,
  "files_per_second": 200.0,
  "peak_files_per_second": 250.0
}
```
**Analysis:** Underutilized (45%), could increase.

### Run 2: Increased scanning to 2000, deletion to 1000
```json
{
  "max_concurrency_scanning": 2000,
  "max_concurrency_deletion": 1000,
  "max_active_tasks": 1200,
  "concurrency_utilization_percent": 60.0,
  "files_per_second": 350.0,
  "peak_files_per_second": 400.0
}
```
**Analysis:** Better utilization (60%), rate increased. Still room to grow.

### Run 3: Increased scanning to 3000, deletion to 2000
```json
{
  "max_concurrency_scanning": 3000,
  "max_concurrency_deletion": 2000,
  "max_active_tasks": 2800,
  "concurrency_utilization_percent": 93.0,
  "files_per_second": 450.0,
  "peak_files_per_second": 480.0
}
```
**Analysis:** Good utilization (93%), rate increased. Close to optimal.

### Run 4: Increased scanning to 4000, deletion to 3000
```json
{
  "max_concurrency_scanning": 4000,
  "max_concurrency_deletion": 3000,
  "max_active_tasks": 3000,
  "concurrency_utilization_percent": 75.0,
  "files_per_second": 460.0,
  "peak_files_per_second": 480.0
}
```
**Analysis:** Utilization dropped (75%), rate barely increased. **Optimal is scanning=3000, deletion=2000.**

## Monitoring Over Time

Watch for these patterns:

### Consistent High Utilization
- Good: Means you're using resources efficiently
- Watch: If rates drop, may be filesystem throttling

### Fluctuating Utilization
- Normal: Different file sizes/types cause variations
- Watch: If correlated with rate drops, may indicate throttling

### Utilization Decreasing Over Time
- Possible: Filesystem slowing down
- Possible: Processing larger files
- Action: Monitor rates - if rates also decreasing, investigate filesystem

## Quick Reference

| Metric | Good Range | Action if Low | Action if High |
|--------|-----------|---------------|----------------|
| `concurrency_utilization_percent` | 70-90% | Increase `--max-concurrency-scanning`/`--max-concurrency-deletion` | Monitor rates - may be optimal or filesystem-limited |
| `files_per_second` | Depends on filesystem | Check utilization, increase concurrency | Good! |
| `peak_files_per_second` vs current | Close (within 20%) | Consistent performance | May have been brief spike |
| `max_active_tasks` vs `max_concurrency` | 70-90% of max | Increase concurrency limits | May be optimal or filesystem-limited |
| `scanning_files_per_second` vs `deletion_files_per_second` | Similar or scanning faster | Normal | If deletion much slower, increase `--max-concurrency-deletion` |

## Environment Variables

For consistency with other parameters, you can set:

```bash
export EFSPURGE_MAX_CONCURRENCY_SCANNING=2000
export EFSPURGE_MAX_CONCURRENCY_DELETION=1000
efspurge /mnt/efs --max-age-days 30
```

**Note:** CLI arguments take precedence over environment variables.

**Deprecated:** `EFSPURGE_MAX_CONCURRENCY` is deprecated but still works (sets both scanning and deletion to the same value). Use separate environment variables for better control.

## Default Values

| Parameter | Default | Environment Variable | Rationale |
|-----------|---------|---------------------|-----------|
| `--max-concurrency-scanning` | 1000 | `EFSPURGE_MAX_CONCURRENCY_SCANNING` | Good balance for most filesystems. For EFS, often needs 2000-3000. |
| `--max-concurrency-deletion` | 1000 | `EFSPURGE_MAX_CONCURRENCY_DELETION` | Good balance for most filesystems. For EFS, often needs 1000-2000. |
| `--max-concurrency` | 1000 (deprecated) | `EFSPURGE_MAX_CONCURRENCY` (deprecated) | Sets both scanning and deletion to same value. Use separate parameters instead. |
| `--max-concurrent-subdirs` | 100 | `EFSPURGE_MAX_CONCURRENT_SUBDIRS` | Prevents memory explosion on deep trees while maintaining good parallelism. |
| `--task-batch-size` | 5000 | None (CLI only) | Balances memory usage with throughput. Lower for memory-constrained environments. |

### Should You Change the Defaults?

**Default `max_concurrency_scanning=1000, max_concurrency_deletion=1000` is appropriate if:**
- You're using AWS EFS (may need scanning=2000-3000, deletion=1000-2000)
- You're using network filesystems (NFS, SMB)
- You want good performance out of the box

**Consider lowering to 500-1000 if:**
- Local disk or fast filesystem
- Memory-constrained environment
- Want more conservative resource usage

**Consider increasing scanning to 2000-3000, deletion to 1000-2000 if:**
- AWS EFS with high latency
- Object storage (S3, etc.)
- You see low utilization (< 50%) with default

**Use the metrics to decide:** After your first run, check `concurrency_utilization_percent`. If consistently < 50%, increase. If consistently > 95%, you may be optimal or filesystem-limited. Also check if `scanning_files_per_second` is much higher than `deletion_files_per_second` - this suggests you can increase deletion concurrency.

### Deprecated Parameters

**`--max-concurrency` and `EFSPURGE_MAX_CONCURRENCY` are deprecated** but still work for backward compatibility. They set both scanning and deletion to the same value. Use `--max-concurrency-scanning`/`--max-concurrency-deletion` and `EFSPURGE_MAX_CONCURRENCY_SCANNING`/`EFSPURGE_MAX_CONCURRENCY_DELETION` instead.

Deprecation warnings will be shown when using the deprecated parameters.

## Troubleshooting

### "Why is my utilization always 100% but rate is low?"

**Answer:** You're filesystem-limited, not concurrency-limited. The filesystem can't handle more operations. Consider:
- Checking filesystem performance
- Reducing concurrency (counterintuitive, but may help if filesystem is overwhelmed)
- Checking for network issues
- Check if scanning vs deletion rates differ - you might need to tune them separately

### "Why is my utilization always low but I want to go faster?"

**Answer:** Check if increasing concurrency actually increases rate. If not, you may be:
- CPU-limited (unlikely for I/O operations)
- Filesystem-limited (filesystem can't go faster)
- Network-limited (for network filesystems)
- Check if scanning is fast but deletion is slow - increase `--max-concurrency-deletion` separately

### "My peak rate is much higher than current rate"

**Answer:** Peak may have been during optimal conditions. Monitor:
- `files_per_second_instant` vs `files_per_second_short`
- If instant rate matches peak, you can achieve it again
- If instant rate is low, something changed (filesystem throttling, file sizes, etc.)
