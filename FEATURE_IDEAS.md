# Feature Ideas: max_depth & Enhanced Rate Metrics

## 🎯 Idea 1: max_depth Parameter for Nested Directories

### Problem Statement
When dealing with very deep directory structures (e.g., 20+ levels), scanning can be:
- Slow (many empty nested directories)
- Memory-intensive (many concurrent directory scans)
- Unnecessary (if you only care about top N levels)

### Proposed Solution
Add a `max_depth` parameter to limit recursion depth.

### Implementation Approaches

#### Approach A: Track Depth in scan_directory()
**Pros**: Simple, minimal changes
**Cons**: Need to pass depth through recursive calls

```python
async def scan_directory(self, directory: Path, depth: int = 0) -> None:
    """
    Recursively scan a directory and process files.
    
    Args:
        directory: Directory path to scan
        depth: Current recursion depth (0 = root)
    """
    # Check depth limit
    if self.max_depth > 0 and depth >= self.max_depth:
        self.logger.debug(f"Skipping {directory} - max depth {self.max_depth} reached")
        await self.update_stats(dirs_skipped_max_depth=1)
        return
    
    # ... existing scanning code ...
    
    # Process subdirectories with incremented depth
    if subdirs:
        for i in range(0, len(subdirs), self.max_concurrent_subdirs):
            batch_subdirs = subdirs[i : i + self.max_concurrent_subdirs]
            subdir_tasks = [
                self.scan_directory(subdir, depth + 1) 
                for subdir in batch_subdirs
            ]
            await asyncio.gather(*subdir_tasks, return_exceptions=True)
```

#### Approach B: Calculate Depth from Path
**Pros**: No need to pass depth parameter
**Cons**: Requires root_path to be accessible, path calculation overhead

```python
def _get_depth(self, directory: Path) -> int:
    """Calculate depth relative to root_path."""
    try:
        relative = directory.relative_to(self.root_path)
        return len(relative.parts) - 1  # -1 because parts[0] is usually empty
    except ValueError:
        # Path is outside root_path (shouldn't happen, but handle gracefully)
        return 0

async def scan_directory(self, directory: Path) -> None:
    # Check depth limit
    depth = self._get_depth(directory)
    if self.max_depth > 0 and depth >= self.max_depth:
        self.logger.debug(f"Skipping {directory} - depth {depth} >= max_depth {self.max_depth}")
        await self.update_stats(dirs_skipped_max_depth=1)
        return
    
    # ... rest of scanning code ...
```

#### Approach C: Use os.walk-style Depth Tracking (if switching to os.walk)
**Pros**: Built-in depth tracking
**Cons**: Would require major refactoring from async_scandir

```python
# This would require switching from async_scandir to async os.walk
# Not recommended - would lose current async benefits
```

### Recommended: Approach A (Explicit Depth Parameter)
- Clean and explicit
- Easy to test
- Minimal performance overhead
- Clear intent

### CLI Integration
```python
parser.add_argument(
    "--max-depth",
    type=int,
    default=0,  # 0 = unlimited
    help="Maximum directory depth to scan (0 = unlimited, default: 0)",
)
```

### Stats to Track
- `dirs_skipped_max_depth`: Count of directories skipped due to depth limit
- `max_depth_reached`: Boolean flag indicating if depth limit was hit

### Edge Cases to Consider
1. **Depth 0**: Should scan root directory only (no subdirs)
2. **Negative depth**: Invalid, raise ValueError
3. **Very large depth**: Performance impact? Probably fine
4. **Symlinks**: Already skipped, so depth doesn't apply
5. **Empty dirs**: If max_depth prevents scanning, should we still check for empty dirs?

---

## 📊 Idea 2: Enhanced Rate Metrics Logging

### Current State
Currently logs:
- `files_per_second`: Overall average rate
- Basic progress updates every 30 seconds

### Proposed Enhancements

### 2.1 Per-Phase Rate Metrics

Track rates separately for each phase:
- **Scanning phase**: Files scanned per second, dirs scanned per second
- **Deletion phase**: Files deleted per second
- **Empty dir removal phase**: Empty dirs deleted per second

```python
class RateTracker:
    """Track rates for different phases and time windows."""
    
    def __init__(self):
        self.phase_rates = {
            "scanning": {"files": [], "dirs": []},
            "deletion": {"files": []},
            "removing_empty_dirs": {"dirs": []},
        }
        self.window_size = 60  # seconds
        self.samples = []  # (timestamp, phase, metric_type, count)
    
    def record(self, phase: str, metric_type: str, count: int):
        """Record a metric sample."""
        self.samples.append((time.time(), phase, metric_type, count))
        # Keep only last window_size seconds
        cutoff = time.time() - self.window_size
        self.samples = [s for s in self.samples if s[0] > cutoff]
    
    def get_rate(self, phase: str, metric_type: str, window_seconds: int = 60) -> float:
        """Calculate rate for a specific phase/metric over time window."""
        cutoff = time.time() - window_seconds
        relevant = [
            s for s in self.samples
            if s[0] > cutoff and s[1] == phase and s[2] == metric_type
        ]
        if not relevant:
            return 0.0
        total = sum(s[3] for s in relevant)
        time_span = relevant[-1][0] - relevant[0][0] if len(relevant) > 1 else 1.0
        return total / time_span if time_span > 0 else 0.0
```

### 2.2 Time-Windowed Rates (Rolling Averages)

Track rates over different time windows:
- **Instant rate**: Last 10 seconds
- **Short-term rate**: Last 60 seconds
- **Overall rate**: Since start

```python
progress_data = {
    # ... existing fields ...
    
    # Instant rates (last 10 seconds)
    "files_per_second_instant": self.rate_tracker.get_rate("scanning", "files", 10),
    "dirs_per_second_instant": self.rate_tracker.get_rate("scanning", "dirs", 10),
    
    # Short-term rates (last 60 seconds)
    "files_per_second_short": self.rate_tracker.get_rate("scanning", "files", 60),
    "dirs_per_second_short": self.rate_tracker.get_rate("scanning", "dirs", 60),
    
    # Overall rates (since start)
    "files_per_second_overall": self.stats["files_scanned"] / elapsed,
    "dirs_per_second_overall": self.stats["dirs_scanned"] / elapsed,
}
```

### 2.3 Peak Rate Tracking

Track maximum rates achieved:
- Peak files/second
- Peak dirs/second
- When peak occurred

```python
self.peak_rates = {
    "files_per_second": {"value": 0.0, "timestamp": None},
    "dirs_per_second": {"value": 0.0, "timestamp": None},
}

# In progress reporter:
current_rate = self.stats["files_scanned"] / elapsed
if current_rate > self.peak_rates["files_per_second"]["value"]:
    self.peak_rates["files_per_second"] = {
        "value": current_rate,
        "timestamp": current_time,
    }
```

### 2.4 Per-Directory-Type Metrics

Track rates for different directory characteristics:
- **Shallow dirs** (1-2 levels): Fast scanning
- **Deep dirs** (10+ levels): Slower scanning
- **Dense dirs** (many files): High file processing rate
- **Sparse dirs** (few files): Lower file processing rate

```python
# Track directory characteristics
self.dir_stats = {
    "shallow_dirs": 0,  # depth <= 2
    "deep_dirs": 0,     # depth > 10
    "dense_dirs": 0,    # > 100 files
    "sparse_dirs": 0,   # < 10 files
}
```

### 2.5 Throughput Metrics

Track data throughput (if file sizes are available):
- **Bytes scanned per second**
- **Bytes deleted per second**
- **Average file size**

```python
# Add to stats
self.stats["bytes_scanned"] = 0
self.stats["bytes_deleted"] = 0

# In process_file:
stat = await aiofiles.os.stat(file_path)
file_size = stat.st_size
await self.update_stats(bytes_scanned=file_size)

# In progress:
progress_data["bytes_per_second"] = self.stats["bytes_scanned"] / elapsed
progress_data["avg_file_size_bytes"] = (
    self.stats["bytes_scanned"] / self.stats["files_scanned"]
    if self.stats["files_scanned"] > 0
    else 0
)
```

### 2.6 Concurrency Efficiency Metrics

Track how efficiently concurrency is being used:
- **Active tasks**: Current number of concurrent operations
- **Concurrency utilization**: active_tasks / max_concurrency
- **Task queue depth**: Pending tasks

```python
# Track active tasks
self.active_tasks = 0
self.max_active_tasks = 0

# In process_file:
async def process_file(self, file_path: Path) -> None:
    self.active_tasks += 1
    self.max_active_tasks = max(self.max_active_tasks, self.active_tasks)
    try:
        # ... process file ...
    finally:
        self.active_tasks -= 1

# In progress:
progress_data["active_tasks"] = self.active_tasks
progress_data["max_active_tasks"] = self.max_active_tasks
progress_data["concurrency_utilization_percent"] = (
    (self.active_tasks / self.max_concurrency) * 100
    if self.max_concurrency > 0
    else 0
)
```

### 2.7 Rate Trend Analysis

Track if rates are improving or degrading:
- **Rate trend**: Increasing, decreasing, stable
- **Rate acceleration**: Rate of change of rate

```python
class RateTrendTracker:
    def __init__(self):
        self.rate_history = []  # (timestamp, rate)
        self.history_window = 300  # 5 minutes
    
    def add_sample(self, rate: float):
        now = time.time()
        self.rate_history.append((now, rate))
        # Keep only last window
        cutoff = now - self.history_window
        self.rate_history = [(t, r) for t, r in self.rate_history if t > cutoff]
    
    def get_trend(self) -> str:
        """Returns 'increasing', 'decreasing', or 'stable'."""
        if len(self.rate_history) < 2:
            return "stable"
        
        recent = self.rate_history[-10:]  # Last 10 samples
        if len(recent) < 2:
            return "stable"
        
        rates = [r for _, r in recent]
        first_half = rates[:len(rates)//2]
        second_half = rates[len(rates)//2:]
        
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        
        if avg_second > avg_first * 1.1:  # 10% increase
            return "increasing"
        elif avg_second < avg_first * 0.9:  # 10% decrease
            return "decreasing"
        else:
            return "stable"
```

### 2.8 ETA (Estimated Time to Completion)

Calculate ETA based on current rates:
- **Files remaining**: files_to_purge - files_purged
- **ETA**: remaining / current_rate

```python
# In progress reporter:
files_remaining = self.stats["files_to_purge"] - self.stats["files_purged"]
if files_remaining > 0 and rate > 0:
    eta_seconds = files_remaining / rate
    progress_data["eta_seconds"] = round(eta_seconds, 1)
    progress_data["eta_formatted"] = format_duration(eta_seconds)
```

### 2.9 Percentile Metrics

Track percentile rates (P50, P95, P99) for more detailed analysis:
- Requires storing rate samples over time
- Useful for identifying performance spikes/drops

```python
def calculate_percentiles(self, rates: list[float]) -> dict:
    """Calculate percentiles from rate samples."""
    if not rates:
        return {}
    sorted_rates = sorted(rates)
    return {
        "p50": sorted_rates[len(sorted_rates) * 50 // 100],
        "p95": sorted_rates[len(sorted_rates) * 95 // 100],
        "p99": sorted_rates[len(sorted_rates) * 99 // 100],
    }
```

### 2.10 Rate Comparison Metrics

Compare current rate to historical/baseline:
- **vs baseline**: Compare to expected rate
- **vs previous run**: If we had previous run data
- **vs theoretical max**: Compare to max_concurrency

```python
progress_data["rate_vs_max_percent"] = (
    (rate / self.max_concurrency) * 100
    if self.max_concurrency > 0
    else 0
)
```

---

## 🎨 Implementation Priority Recommendations

### High Priority (Quick Wins)
1. ✅ **Per-phase rates** - Easy to implement, high value
2. ✅ **Peak rate tracking** - Simple, useful for performance analysis
3. ✅ **Time-windowed rates** - Helps identify performance trends

### Medium Priority (Moderate Effort)
4. ✅ **max_depth parameter** - Useful feature, moderate complexity
5. ✅ **Concurrency efficiency metrics** - Helps optimize concurrency settings
6. ✅ **ETA calculation** - User-friendly feature

### Lower Priority (More Complex)
7. ⚠️ **Throughput metrics** - Requires file size tracking (extra I/O)
8. ⚠️ **Rate trend analysis** - More complex, requires history tracking
9. ⚠️ **Percentile metrics** - Requires storing many samples

---

## 📝 Example Enhanced Progress Log

```json
{
  "timestamp": "2026-01-18 15:30:45",
  "level": "INFO",
  "message": "Progress update",
  "extra_fields": {
    "phase": "scanning",
    
    // Overall metrics
    "files_scanned": 50000,
    "files_to_purge": 12000,
    "files_purged": 0,
    "dirs_scanned": 5000,
    "elapsed_seconds": 120.5,
    
    // Rate metrics - overall
    "files_per_second_overall": 415.2,
    "dirs_per_second_overall": 41.5,
    
    // Rate metrics - instant (last 10s)
    "files_per_second_instant": 450.0,
    "dirs_per_second_instant": 45.0,
    
    // Rate metrics - short-term (last 60s)
    "files_per_second_short": 420.0,
    "dirs_per_second_short": 42.0,
    
    // Peak rates
    "peak_files_per_second": 500.0,
    "peak_files_per_second_at": "2026-01-18 15:25:30",
    
    // Concurrency metrics
    "active_tasks": 850,
    "max_active_tasks": 950,
    "concurrency_utilization_percent": 85.0,
    
    // ETA
    "files_remaining": 12000,
    "eta_seconds": 28.9,
    "eta_formatted": "29s",
    
    // Rate trend
    "rate_trend": "increasing",
    
    // Memory
    "memory_mb": 150.5,
    "memory_limit_mb": 800,
    "memory_usage_percent": 18.8
  }
}
```

---

## 🔧 Implementation Notes

### For max_depth:
- Default to 0 (unlimited) for backward compatibility
- Add validation: `if max_depth < 0: raise ValueError`
- Consider logging when depth limit is hit (DEBUG level)
- Update tests to cover depth limiting

### For Rate Metrics:
- Start with simple additions (per-phase, peak, time-windowed)
- Add more complex metrics incrementally
- Consider making rate tracking optional/configurable (performance overhead)
- Use efficient data structures (deque for time-windowed samples)

### Performance Considerations:
- Rate tracking adds minimal overhead (just arithmetic)
- Time-windowed samples need periodic cleanup (every N samples, not every sample)
- Consider making detailed metrics opt-in via flag

---

## 🧪 Testing Ideas

### For max_depth:
- Test with depth=0 (should scan root only)
- Test with depth=1 (should scan root + 1 level)
- Test with very deep structure (20+ levels)
- Test with depth > actual depth (should work normally)
- Test empty dir removal with max_depth

### For Rate Metrics:
- Test with fast processing (high rates)
- Test with slow processing (low rates)
- Test phase transitions (scanning -> deletion)
- Test with very short runs (< 10 seconds)
- Test with very long runs (hours)
- Test rate trend detection
