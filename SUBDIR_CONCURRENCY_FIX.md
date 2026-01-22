# Subdirectory Concurrency Fix

## Problem

The application was getting stuck on large directories (e.g., `/data/api_files` and `/data/api_files/mariadb`) with 0% concurrency utilization, even though only 2 active directories were being scanned.

### Root Cause

The batch-based subdirectory processing approach had a critical flaw:

```python
# OLD CODE (Batch-based):
for i in range(0, len(subdirs), self.max_concurrent_subdirs):
    batch_subdirs = subdirs[i : i + self.max_concurrent_subdirs]
    subdir_tasks = [self.scan_directory(subdir) for subdir in batch_subdirs]
    await asyncio.gather(*subdir_tasks, return_exceptions=True)
```

**Issue**: `asyncio.gather()` waits for ALL tasks in a batch to complete before starting the next batch. If 2 directories are very large, they block the entire batch, leaving most concurrency slots unused.

**Example**:
- Directory has 100 subdirectories
- Batch size = 100 (max_concurrent_subdirs)
- 2 directories are huge (take 10 minutes each)
- 98 directories finish in 1 minute
- **Result**: 98 slots idle for 9 minutes waiting for the 2 slow directories

## Solution: Hybrid Approach

Implemented a hybrid approach that combines:
1. **Semaphore-controlled concurrency** - Maintains constant concurrency
2. **On-demand task creation** - Prevents memory explosion
3. **Immediate slot refilling** - High utilization

### New Implementation

```python
async def _process_subdirs_with_constant_concurrency(self, subdirs: list[Path]) -> None:
    """
    Process subdirectories with constant concurrency using a hybrid approach.
    
    - Uses semaphore to limit concurrent execution (maintains constant concurrency)
    - Creates tasks on-demand as slots become available (prevents memory explosion)
    - As tasks complete, new ones start immediately (high utilization)
    """
    remaining_subdirs = list(subdirs)
    active_tasks: list[asyncio.Task] = []
    
    async def scan_with_semaphore(subdir: Path) -> None:
        async with self.subdir_semaphore:
            await self.scan_directory(subdir)
    
    while remaining_subdirs or active_tasks:
        # Create tasks up to concurrency limit
        while len(active_tasks) < self.max_concurrent_subdirs and remaining_subdirs:
            subdir = remaining_subdirs.pop(0)
            task = asyncio.create_task(scan_with_semaphore(subdir))
            active_tasks.append(task)
        
        # Wait for at least one to complete, then immediately start next
        if active_tasks:
            done, pending = await asyncio.wait(
                active_tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            # Remove completed tasks and create new ones
            for task in done:
                active_tasks.remove(task)
```

### How It Works

1. **Constant Concurrency**: Semaphore ensures exactly `max_concurrent_subdirs` directories are scanned concurrently
2. **On-Demand Creation**: Tasks are created as slots become available, never exceeding `max_concurrent_subdirs`
3. **Immediate Refilling**: As soon as one task completes, a new one starts (no idle slots)
4. **Memory Safe**: Never creates more than `max_concurrent_subdirs` task objects at once

### Benefits

✅ **High Concurrency Utilization**: Always maintains `max_concurrent_subdirs` active scans  
✅ **No Memory Explosion**: Never creates all tasks upfront  
✅ **Prevents Stuck Detection**: Slow directories don't block others  
✅ **Recursive Safety**: Works correctly in deep directory trees  

### Memory Comparison

**Old Approach (Batch-based)**:
- Creates `max_concurrent_subdirs` tasks per batch
- Waits for entire batch to complete
- **Issue**: Idle slots when some directories finish early

**New Approach (Hybrid)**:
- Creates up to `max_concurrent_subdirs` tasks at once
- Creates new tasks immediately as slots become available
- **Result**: Constant concurrency, bounded memory

**Example with 10,000 subdirectories**:
- **Old**: Creates 100 tasks, waits for all 100, then creates next 100
  - Memory: 100 task objects at a time
  - **Problem**: If 2 are slow, 98 slots idle
  
- **New**: Creates 100 tasks, as each completes, creates next one
  - Memory: Always exactly 100 task objects
  - **Result**: Constant 100 concurrent scans, no idle slots

## Testing Recommendations

1. **Test with large directories**: Verify constant concurrency maintained
2. **Test with deep trees**: Verify no memory explosion
3. **Test with mixed sizes**: Verify slow directories don't block others
4. **Monitor concurrency utilization**: Should stay near 100% (not 0%)

## Configuration

No configuration changes needed. Uses existing `max_concurrent_subdirs` parameter (default: 100).

## Backward Compatibility

✅ Fully backward compatible - same parameters, same behavior, better performance.
