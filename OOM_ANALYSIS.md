# 🔴 OOM (Out of Memory) Analysis - Cron Run Failure

## Executive Summary

**Status**: ❌ **FAILED** - Empty directories were **NOT** fully truncated due to OOM restarts

**Key Findings**:
1. **Two distinct cron runs** both failed due to OOM during `removing_empty_dirs` phase
2. **Memory exceeded 90%** of 4500 MB limit without triggering back-pressure (`memory_backpressure_events: 0`)
3. **"POSSIBLE HANG DETECTED"** warnings indicate filesystem slowness/unresponsiveness
4. **Unlimited empty directory deletion** (`max_empty_dirs_to_delete: 0`) allowed unbounded memory growth
5. **Neither run completed** - both were killed by Kubernetes OOMKiller before completion

---

## Detailed Analysis

### Run 1: Initial Attempt

**Pod**: `crn-efs-purge-api-files-29492831-<pod-id-1>`

**Configuration**:
- `memory_limit_mb`: 4500 MB
- `max_empty_dirs_to_delete`: 0 (unlimited)
- `task_batch_size`: 10000
- `max_concurrency_deletion`: (likely 1000, default)

**Timeline**:
1. ✅ Scanning phase completed successfully
2. ⚠️ Started `removing_empty_dirs` phase
3. 📈 Memory usage climbed to **>90%** of limit (~4050+ MB)
4. ⚠️ "POSSIBLE HANG DETECTED" warnings appeared with increasing `stuck_intervals` (60s, 90s, 120s, 150s, 180s, 210s, 240s, 270s...)
5. 🔴 **OOMKilled** by Kubernetes before completion
6. ❌ **No completion message** - job terminated prematurely

**Critical Issue**: `memory_backpressure_events: 0` despite memory exceeding limit

### Run 2: Retry After Restart

**Pod**: `crn-efs-purge-api-files-29492831-<pod-id-2>`

**Timeline**:
1. ✅ Started fresh scan
2. ⚠️ Multiple `FileNotFoundError` during scanning (directories already deleted by Run 1)
3. ⚠️ Started `removing_empty_dirs` phase
4. 📈 Memory usage again climbed to **>90%** of limit
5. ⚠️ "POSSIBLE HANG DETECTED" warnings with increasing `stuck_intervals`
6. 🔴 **OOMKilled** again before completion
7. ❌ **No completion message** - job terminated prematurely

**Critical Issue**: Same pattern - `memory_backpressure_events: 0` despite high memory

---

## Root Cause Analysis

### Primary Cause: Memory Back-Pressure Not Triggering

**Problem**: Despite memory exceeding 90% of the 4500 MB limit, `memory_backpressure_events` remained at 0.

**Why This Happened**:

1. **Memory Check Timing Issue**:
   - `check_memory_pressure()` is called **before** batches (line 772, 930, 1031)
   - Memory spikes occur **during** `asyncio.gather()` execution (line 822, 1049)
   - Checks happen **after** batches complete (line 826, 1052), but by then memory may have already spiked beyond recoverable limits

2. **Lock Contention**:
   - `check_memory_pressure()` uses `memory_check_lock` (line 519)
   - If many concurrent tasks are running, the lock may prevent timely checks
   - The 0.5s sleep under lock (line 536) could delay other operations

3. **Memory Measurement Delay**:
   - `get_memory_usage_mb()` uses `psutil` which may not reflect immediate spikes
   - Python's garbage collector runs asynchronously
   - Memory may spike faster than checks can detect

4. **Batch Size Too Large**:
   - Initial batch size: `min(200, max(50, max_concurrency_deletion // 10))` (line 768)
   - With `max_concurrency_deletion=1000`, this gives batch size of 200
   - 200 concurrent directory deletions can create significant memory pressure
   - Dynamic reduction (lines 780-789) may not be aggressive enough

### Secondary Cause: Filesystem Slowness Leading to Memory Accumulation

**Problem**: "POSSIBLE HANG DETECTED" warnings indicate the filesystem was slow/unresponsive.

**Impact**:
- Slow `rmdir()` operations cause tasks to remain in memory longer
- Concurrent tasks accumulate while waiting for slow I/O
- Memory cannot be freed until operations complete
- With unlimited `max_empty_dirs_to_delete`, this compounds over time

**Evidence**:
- `stuck_intervals` increasing: 60s → 90s → 120s → 150s → 180s → 210s → 240s → 270s
- This indicates no progress for extended periods
- Suggests EFS filesystem was slow or unresponsive

### Tertiary Cause: Unlimited Empty Directory Deletion

**Problem**: `max_empty_dirs_to_delete: 0` means no limit on deletion.

**Impact**:
- With potentially millions of empty directories, memory can grow unbounded
- Even with batching, accumulated state (sets, lists) grows linearly
- Cascading deletion creates new empty parents, extending the process
- No circuit breaker to stop if memory becomes critical

---

## Why Empty Directories Were NOT Fully Truncated

**Answer**: ❌ **NO** - Empty directories were **NOT** fully truncated.

**Evidence**:
1. **No completion message**: Neither run logged "Purge operation completed" or "Empty directory removal completed"
2. **OOMKilled before completion**: Both runs were terminated by Kubernetes OOMKiller
3. **Second run found FileNotFoundError**: Indicates some directories were deleted by Run 1, but not all
4. **Stuck detection warnings**: Process was making no progress before being killed

**Partial Progress**:
- Run 1 likely deleted some empty directories before OOM
- Run 2 encountered `FileNotFoundError` because Run 1 had partially completed
- But neither run completed the full truncation task

---

## Recommended Fixes

### Fix 1: Improve Memory Check Timing (CRITICAL)

**Problem**: Memory checks happen before batches, missing spikes during execution.

**Solution**: Add more frequent checks and improve detection:

```python
# In _remove_empty_directories(), around line 822:
tasks = [remove_single_directory(directory) for directory in batch]

# Add: Monitor memory during batch execution
# Use asyncio.wait() with timeout to check memory periodically
results = []
for task in asyncio.as_completed(tasks, timeout=5.0):
    try:
        result = await task
        results.append(result)
        
        # Check memory every N completions
        if len(results) % 10 == 0:
            memory_high, current_memory_mb = await self.check_memory_pressure()
            if memory_high:
                # Cancel remaining tasks if memory critical
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break
    except asyncio.TimeoutError:
        # Task taking too long - check memory
        memory_high, current_memory_mb = await self.check_memory_pressure()
        if memory_high:
            # Cancel remaining tasks
            for t in tasks:
                if not t.done():
                    t.cancel()
            break
```

**Alternative (Simpler)**: Reduce batch sizes more aggressively when memory is high:

```python
# Line 780-789: More aggressive reduction
if memory_high:
    current_batch_size = max(5, current_batch_size // 8)  # 87.5% reduction (was 75%)
elif memory_percent > 85:  # Lower threshold (was 80%)
    current_batch_size = max(10, current_batch_size // 4)  # 75% reduction (was 50%)
elif memory_percent > 70:  # Lower threshold (was 60%)
    current_batch_size = max(20, current_batch_size // 2)  # 50% reduction (was 25%)
```

### Fix 2: Set Reasonable Limit on Empty Directory Deletion

**Problem**: `max_empty_dirs_to_delete: 0` allows unbounded growth.

**Solution**: Set a reasonable limit based on memory capacity:

```python
# Calculate safe limit based on memory
# Estimate: ~0.1 MB per directory in memory (Path objects, sets, etc.)
# With 4500 MB limit, safe limit: ~45,000 directories per run
# Use 30,000 for safety margin

# In CLI or configuration:
max_empty_dirs_to_delete = 30000  # Instead of 0 (unlimited)
```

**Or**: Make it configurable with a warning:

```python
if max_empty_dirs_to_delete == 0:
    self.logger.warning(
        "max_empty_dirs_to_delete=0 (unlimited) can cause OOM with large numbers of empty directories. "
        "Consider setting a limit based on available memory."
    )
```

### Fix 3: Add Memory-Based Circuit Breaker

**Problem**: No mechanism to stop if memory becomes critical.

**Solution**: Add a circuit breaker that stops processing if memory exceeds a critical threshold:

```python
# In _remove_empty_directories(), add before each batch:
CRITICAL_MEMORY_THRESHOLD = 0.95  # 95% of limit

memory_high, current_memory_mb = await self.check_memory_pressure()
memory_percent = (current_memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0

if memory_percent > CRITICAL_MEMORY_THRESHOLD * 100:
    self.logger.error(
        f"CRITICAL: Memory usage ({memory_percent:.1f}%) exceeds critical threshold. "
        f"Stopping empty directory deletion to prevent OOM. "
        f"Processed {self.stats.get('empty_dirs_deleted', 0)} directories before stopping."
    )
    break  # Stop processing
```

### Fix 4: Improve Stuck Detection and Recovery

**Problem**: "POSSIBLE HANG DETECTED" warnings don't trigger recovery actions.

**Solution**: When stuck detection triggers, reduce concurrency and batch sizes:

```python
# In _background_progress_reporter(), around line 1506:
if self.stuck_detection_count >= 2:
    # Reduce concurrency when stuck
    if self.stuck_detection_count == 2:
        # First stuck detection: reduce batch sizes
        self.logger.warning("Reducing batch sizes due to stuck detection...")
        # This will be picked up in next batch iteration
    
    if self.stuck_detection_count >= 4:
        # Multiple stuck detections: force smaller batches
        self.logger.error(
            "Multiple stuck detections. Consider stopping job or checking filesystem health."
        )
```

### Fix 5: Reduce Initial Batch Sizes

**Problem**: Initial batch size of 200 may be too large for EFS.

**Solution**: Start with smaller batches and increase if memory allows:

```python
# Line 768: More conservative initial batch size
max_batch_size = min(100, max(25, self.max_concurrency_deletion // 20))  # Was // 10
```

---

## Immediate Actions Required

### 1. Set `max_empty_dirs_to_delete` Limit

**Action**: Update your cron job configuration:

```yaml
# In k8s-cronjob.yaml or equivalent
args:
  - --max-empty-dirs-to-delete=30000  # Set reasonable limit instead of 0
```

**Rationale**: Prevents unbounded memory growth with millions of empty directories.

### 2. Increase Memory Limit or Reduce Concurrency

**Option A**: Increase memory limit:
```yaml
resources:
  limits:
    memory: "6Gi"  # Increase from current limit to allow more headroom
```

**Option B**: Reduce concurrency:
```yaml
args:
  - --max-concurrency-deletion=500  # Reduce from 1000 to use less memory
```

**Recommendation**: Do both - increase memory limit AND set `max_empty-dirs-to-delete`.

### 3. Monitor Memory More Closely

**Action**: Add memory monitoring alerts:
- Alert if `memory_usage_percent > 85%`
- Alert if `memory_backpressure_events > 0` (should trigger but currently doesn't)
- Alert if "POSSIBLE HANG DETECTED" appears

### 4. Test Fixes in Staging

**Action**: Before deploying to production:
1. Test with `max_empty_dirs_to_delete=1000` on a small subset
2. Monitor `memory_backpressure_events` - should be > 0 if memory exceeds limit
3. Verify completion message appears
4. Gradually increase limit based on results

---

## Code Changes Applied ✅

### Priority 1 (Critical): Fix Memory Check Timing ✅ FIXED

**File**: `src/efspurge/purger.py`

**Changes Applied**:
1. ✅ **Lowered back-pressure threshold from 100% to 85%** (line 521)
   - Memory spikes during `asyncio.gather()` can push usage from 85% to OOM before checks detect it
   - Triggering at 85% provides safety margin to prevent OOM
   
2. ✅ **More aggressive batch size reduction** (lines 780-789, 937-945, 1034-1043)
   - Memory >85%: 87.5% reduction (was 75%)
   - Memory >80%: 75% reduction (was 50%)
   - Memory >70%: 50% reduction (was 25%, threshold lowered from 60%)
   
3. ✅ **Added circuit breaker for critical memory threshold** (lines 775-786, 932-943, 1034-1043)
   - Stops processing if memory exceeds 95% of limit
   - Prevents OOM by aborting before memory becomes critical
   - Logs error message with progress before stopping
   
4. ✅ **Added warning for unlimited deletion** (lines 410-421)
   - Warns when `max_empty_dirs_to_delete=0` (unlimited)
   - Suggests reasonable limit based on `memory_limit_mb`
   - Helps prevent configuration mistakes

### Priority 2 (High): Add Warnings for Unlimited Deletion

**File**: `src/efspurge/purger.py`

**Changes**:
1. Warn when `max_empty_dirs_to_delete=0` (line 407)
2. Suggest reasonable limit based on `memory_limit_mb`

### Priority 3 (Medium): Improve Stuck Detection Recovery

**File**: `src/efspurge/purger.py`

**Changes**:
1. Reduce batch sizes when stuck detection triggers
2. Add option to abort if stuck for too long

---

## Expected Behavior After Fixes

### Before Fixes:
- ❌ Memory exceeds limit without back-pressure triggering
- ❌ OOMKilled before completion
- ❌ No visibility into why back-pressure didn't work
- ❌ Unlimited deletion allows unbounded growth

### After Fixes:
- ✅ Back-pressure triggers when memory exceeds limit
- ✅ Batch sizes reduce aggressively when memory is high
- ✅ Circuit breaker stops processing if memory critical
- ✅ Reasonable limit prevents unbounded growth
- ✅ Completion message appears when done
- ✅ Empty directories fully truncated (within limit)

---

## Testing Recommendations

### Test 1: Verify Back-Pressure Triggers

```python
# Create test with memory limit that will be exceeded
purger = AsyncEFSPurger(
    root_path="/test",
    max_age_days=30,
    remove_empty_dirs=True,
    memory_limit_mb=100,  # Low limit
    max_empty_dirs_to_delete=10000,  # Enough to exceed limit
)

# Verify memory_backpressure_events > 0 after run
assert purger.stats["memory_backpressure_events"] > 0
```

### Test 2: Verify Circuit Breaker Works

```python
# Create test with very low memory limit
purger = AsyncEFSPurger(
    root_path="/test",
    max_age_days=30,
    remove_empty_dirs=True,
    memory_limit_mb=50,  # Very low
    max_empty_dirs_to_delete=100000,  # Many directories
)

# Verify job stops before OOM
# Check logs for "CRITICAL: Memory usage exceeds critical threshold"
```

### Test 3: Verify Completion with Limit

```python
# Create test with limit
purger = AsyncEFSPurger(
    root_path="/test",
    max_age_days=30,
    remove_empty_dirs=True,
    max_empty_dirs_to_delete=1000,  # Set limit
)

# Verify completion message appears
# Verify empty_dirs_deleted <= max_empty_dirs_to_delete
```

---

## Summary

**Root Cause**: Memory back-pressure mechanism failed to trigger despite memory exceeding 90% of limit, combined with unlimited empty directory deletion and filesystem slowness.

**Impact**: Empty directories were **NOT** fully truncated - both runs were OOMKilled before completion.

**Fixes Applied** ✅:
1. ✅ **CRITICAL**: Lowered back-pressure threshold to 85% and added circuit breaker at 95%
2. ✅ **CRITICAL**: More aggressive batch size reduction when memory is high
3. ✅ **HIGH**: Added warning for unlimited `max_empty_dirs_to_delete` configuration

**Next Steps**:
1. ✅ Code fixes applied (Priority 1)
2. ⏳ **ACTION REQUIRED**: Update configuration (set `max_empty_dirs_to_delete` to reasonable limit)
3. ⏳ Test in staging with new fixes
4. ⏳ Deploy to production with monitoring

**Expected Behavior After Fixes**:
- ✅ Back-pressure triggers at 85% (instead of 100%)
- ✅ Circuit breaker stops processing at 95% to prevent OOM
- ✅ Batch sizes reduce more aggressively when memory is high
- ✅ Warning alerts users to set `max_empty_dirs_to_delete` limit
- ✅ Empty directories will be truncated (within limit) without OOM

---

**Generated**: 2026-01-28  
**Analysis Based On**: Production cron run logs showing OOM events
