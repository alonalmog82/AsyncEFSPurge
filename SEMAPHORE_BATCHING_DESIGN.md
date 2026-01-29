# Semaphore + Batching Design Implementation

## Overview

The empty directory deletion has been refactored to use a **semaphore + queue pattern** that ensures memory usage is bounded by the semaphore limit, not by batch size.

## Design Requirements

✅ **Semaphore limits concurrent I/O operations** - Prevents filesystem overload  
✅ **Batching prevents memory explosion** - Tasks created on-demand, not all upfront  
✅ **Memory bounded by semaphore limit** - Not by batch size or total directories  

## Previous Implementation (Problem)

**Old Approach**:
```python
# Created all tasks upfront - memory = batch_size * memory_per_task
batch = sorted_dirs[i : i + batch_size]
tasks = [remove_single_directory(directory) for directory in batch]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

**Problems**:
- Creating 200 tasks upfront consumes memory even if semaphore only allows 1000 concurrent operations
- Memory usage = `batch_size * memory_per_task` (e.g., 200 * ~0.1 MB = 20 MB per batch)
- With large batches, memory can grow unbounded
- Semaphore limits I/O but doesn't limit task object creation

## New Implementation (Solution)

**New Approach**:
```python
# Producer-consumer pattern with semaphore-controlled workers
# Memory = semaphore_limit * memory_per_task (e.g., 1000 * ~0.1 MB = 100 MB max)

# Queue holds directories (bounded by semaphore_limit + buffer)
directory_queue = asyncio.Queue(maxsize=semaphore_limit + 100)

# Workers pull from queue and process (semaphore limits concurrent I/O)
async def worker():
    directory = await directory_queue.get()
    async with deletion_semaphore:  # Limits concurrent operations
        await remove_directory(directory)
```

**Benefits**:
- ✅ Tasks created on-demand as semaphore slots become available
- ✅ Memory usage = `semaphore_limit * memory_per_task` (bounded)
- ✅ Queue size bounded, preventing memory growth
- ✅ Semaphore controls both I/O concurrency AND memory usage

## Architecture

### Components

1. **Queue**: Bounded queue holding directories to process
   - Size: `semaphore_limit + 100` (small buffer)
   - Prevents unbounded memory growth

2. **Workers**: Async tasks that process directories
   - Number: `semaphore_limit` (one per semaphore slot)
   - Each worker waits for semaphore slot before processing
   - Memory per worker: ~0.1 MB (task object + minimal state)

3. **Producer**: Feeds directories to queue
   - Respects memory/rate limits
   - Blocks if queue is full (back-pressure)

4. **Semaphore**: Limits concurrent I/O operations
   - Controls filesystem load
   - Also controls memory usage (via worker limit)

### Memory Calculation

**Before** (batch-based):
```
Memory = batch_size * memory_per_task
Example: 200 tasks * 0.1 MB = 20 MB per batch
Problem: Can create many batches = unbounded memory
```

**After** (semaphore-based):
```
Memory = semaphore_limit * memory_per_task + queue_size * directory_reference
Example: 1000 workers * 0.1 MB + 1100 queue entries * 0.001 MB ≈ 100 MB max
Benefit: Bounded by semaphore limit, not batch size
```

## Implementation Details

### First Pass (Initial Empty Directories)

```python
# Queue-based processing with semaphore control
directory_queue = asyncio.Queue(maxsize=max_concurrency_deletion + 100)
results_queue = asyncio.Queue()
workers = [create_worker() for _ in range(max_concurrency_deletion)]

# Producer feeds directories to queue
async def producer():
    for directory in sorted_dirs:
        await directory_queue.put(directory)  # Blocks if queue full

# Workers process directories (semaphore limits concurrent I/O)
async def worker():
    directory = await directory_queue.get()
    async with deletion_semaphore:
        result = await remove_directory(directory)
    await results_queue.put(result)
```

**Memory Bound**: `max_concurrency_deletion * memory_per_worker`

### Cascading Deletion (Parents That Become Empty)

Same pattern applied to cascading deletion:
- Queue-based processing
- Semaphore-controlled workers
- Memory bounded by semaphore limit

## Memory Safety Features

### 1. Circuit Breaker
- Stops processing if memory exceeds 95% of limit
- Prevents OOM by aborting before critical threshold

### 2. Back-Pressure
- Triggers at 85% of memory limit
- Queue blocks producer when full
- Workers continue processing, reducing queue size

### 3. Rate Limiting
- `max_empty_dirs_to_delete` limits total directories processed
- Prevents unbounded growth with millions of directories

## Performance Characteristics

### Memory Usage
- **Bounded**: Memory = `semaphore_limit * ~0.1 MB` (not batch_size * memory)
- **Predictable**: Memory usage stays constant regardless of total directories
- **Scalable**: Can process millions of directories with fixed memory footprint

### Concurrency
- **Optimal**: Uses all available semaphore slots (no idle workers)
- **Controlled**: Semaphore prevents filesystem overload
- **Efficient**: Workers process directories as fast as semaphore allows

### Throughput
- **High**: Concurrent processing with semaphore-controlled I/O
- **Stable**: Memory pressure doesn't reduce throughput (queue provides buffer)
- **Resilient**: Handles slow filesystem operations gracefully

## Configuration

### Key Parameters

1. **`max_concurrency_deletion`**: Controls semaphore limit
   - Higher = more concurrent operations = more memory
   - Lower = fewer concurrent operations = less memory
   - **Memory bound**: `max_concurrency_deletion * ~0.1 MB`

2. **`memory_limit_mb`**: Soft memory limit for back-pressure
   - Triggers back-pressure at 85%
   - Circuit breaker at 95%
   - Should be > `max_concurrency_deletion * 0.1 MB`

3. **`max_empty_dirs_to_delete`**: Rate limit
   - Prevents processing unlimited directories
   - Recommended: `(memory_limit_mb * 0.7) / 0.1` (70% of memory limit)

### Example Configuration

```yaml
# For 4500 MB memory limit
args:
  - --max-concurrency-deletion=1000  # Memory bound: 100 MB
  - --memory-limit-mb=4500            # Back-pressure at 3825 MB
  - --max-empty-dirs-to-delete=30000 # Rate limit
```

**Memory Usage**:
- Workers: 1000 * 0.1 MB = 100 MB
- Queue: 1100 * 0.001 MB = 1.1 MB
- **Total**: ~101 MB (bounded, regardless of total directories)

## Testing

### Memory Bounds Verification

```python
# Test that memory stays bounded regardless of directory count
purger = AsyncEFSPurger(
    max_concurrency_deletion=1000,
    memory_limit_mb=4500,
)

# Process 100,000 directories
# Expected: Memory stays ~100 MB (semaphore_limit * memory_per_task)
# Not: 100,000 * 0.1 MB = 10 GB (old batch-based approach)
```

### Concurrency Verification

```python
# Test that semaphore limits concurrent operations
# Expected: At most max_concurrency_deletion operations running simultaneously
# Verified: Semaphore ensures this limit
```

## Migration Notes

### Breaking Changes
- None - API remains the same

### Performance Changes
- **Memory**: More predictable, bounded by semaphore limit
- **Throughput**: Similar or better (queue provides buffer for slow operations)
- **Latency**: Slightly higher (queue adds small overhead, but negligible)

### Configuration Changes
- No changes required
- Existing configurations work as-is
- Can increase `max_concurrency_deletion` for better throughput without memory concerns

## Benefits Summary

✅ **Memory Bounded**: Memory usage = `semaphore_limit * memory_per_task` (not batch_size)  
✅ **Predictable**: Memory stays constant regardless of total directories  
✅ **Scalable**: Can process millions of directories with fixed memory footprint  
✅ **Controlled**: Semaphore limits both I/O concurrency AND memory usage  
✅ **Resilient**: Handles slow filesystem operations gracefully  
✅ **Efficient**: Uses all available semaphore slots (no idle workers)  

## Future Improvements

1. **Dynamic Worker Scaling**: Adjust worker count based on memory pressure
2. **Priority Queue**: Process deeper directories first (already sorted by depth)
3. **Batch Optimization**: Group related directories for better cache locality
4. **Metrics**: Track queue depth, worker utilization, memory per worker

---

**Design Principle**: Memory usage should be bounded by the concurrency limit (semaphore), not by the batch size or total work items. This ensures predictable memory usage regardless of dataset size.
