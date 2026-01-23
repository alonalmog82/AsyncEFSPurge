# AsyncEFSPurge

High-performance asynchronous file purger designed for AWS EFS and network filesystems with millions of files.

[![CI](https://github.com/alonalmog82/AsyncEFSPurge/workflows/CI/badge.svg)](https://github.com/alonalmog82/AsyncEFSPurge/actions)
[![Docker](https://github.com/alonalmog82/AsyncEFSPurge/workflows/Docker/badge.svg)](https://github.com/alonalmog82/AsyncEFSPurge/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Features

- ⚡ **High Performance** - Async I/O with configurable concurrency (handles 1000+ files/sec)
  - **Concurrent subdirectory scanning** - Process directory trees in parallel
  - **Batched task creation** - Prevents OOM on large directories
  - **Memory back-pressure** - Automatic throttling when memory is high
- 🔒 **Safe** - Dry-run mode, symlink handling, comprehensive error handling
- 📊 **Observable** - JSON structured logging for Kubernetes/CloudWatch
- 🎯 **Flexible** - Age-based filtering with day precision
- 🐳 **Production Ready** - Docker support with security best practices
- 📈 **Detailed Stats** - Files scanned, purged, bytes freed, errors, and performance metrics
- 🗂️ **Empty Directory Cleanup** - Optional post-order deletion of empty directories with rate limiting

## Quick Start

### Using Docker (Recommended)

```bash
# Pull from GitHub Container Registry
docker pull ghcr.io/alonalmog82/asyncefspurge:latest

# Dry run to see what would be deleted
docker run --rm -v /mnt/efs:/data ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --max-age-days 30 --dry-run

# Actually delete files
docker run --rm -v /mnt/efs:/data ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --max-age-days 30
```

### Using Python

```bash
# Install
pip install -e .

# Dry run
efspurge /mnt/efs --max-age-days 30 --dry-run

# Purge files older than 30 days
efspurge /mnt/efs --max-age-days 30

# High concurrency for network storage (separate limits for scanning and deletion)
efspurge /mnt/efs --max-age-days 7 --max-concurrency-scanning 2000 --max-concurrency-deletion 1000
```

## Installation

### Option 1: From Source

```bash
git clone https://github.com/alonalmog82/AsyncEFSPurge.git
cd AsyncEFSPurge
pip install -e .
```

### Option 2: Docker

```bash
docker build -t efspurge:latest .
```

### Option 3: GitHub Container Registry

```bash
docker pull ghcr.io/alonalmog82/asyncefspurge:latest
```

## Usage

### Command Line Arguments

```
efspurge [path] [options]

positional arguments:
  path                  Root path to scan and purge

options:
  --max-age-days DAYS       Files older than this (in days) will be purged (default: 30.0)
  --max-concurrency N       [DEPRECATED] Maximum concurrent async operations (use --max-concurrency-scanning/deletion)
  --max-concurrency-scanning N  Maximum concurrent file scanning (stat) operations (default: 1000)
  --max-concurrency-deletion N  Maximum concurrent file deletion (remove) operations (default: 1000)
  --memory-limit-mb MB      Soft memory limit in MB, triggers back-pressure (default: 800)
  --task-batch-size N       Maximum tasks to create at once, prevents OOM (default: 5000)
  --max-concurrent-subdirs N  Maximum subdirectories to scan concurrently (default: 100)
  --dry-run                 Don't actually delete files, just report what would be deleted
  --remove-empty-dirs       Remove empty directories after scanning (post-order deletion)
  --max-empty-dirs-to-delete N  Maximum empty directories to delete per run (0 = unlimited, default: 500)
  --log-level LEVEL         Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
  --version                 Show version and exit
  -h, --help                Show this help message and exit
```

### Examples

**Dry run to preview deletions:**
```bash
efspurge /mnt/efs/data --max-age-days 30 --dry-run
```

**Purge files older than 90 days:**
```bash
efspurge /mnt/efs/old-files --max-age-days 90
```

**High-performance mode for very large filesystems:**
```bash
efspurge /mnt/efs --max-age-days 7 --max-concurrency-scanning 2000 --max-concurrency-deletion 1000 --memory-limit-mb 1600
```

**Debug mode with detailed logging:**
```bash
efspurge /mnt/efs/temp --max-age-days 1 --log-level DEBUG
```

**Remove empty directories after purging:**
```bash
efspurge /mnt/efs --max-age-days 30 --remove-empty-dirs
```

**Rate-limited empty directory removal (default: 500/run):**
```bash
# Default: removes up to 500 empty directories per run
efspurge /mnt/efs --max-age-days 30 --remove-empty-dirs

# First run with many empty directories: use unlimited
efspurge /mnt/efs --max-age-days 30 --remove-empty-dirs --max-empty-dirs-to-delete 0

# Custom rate limit: 100 directories per run for very gradual cleanup
efspurge /mnt/efs --max-age-days 30 --remove-empty-dirs --max-empty-dirs-to-delete 100
```

## Deployment

### Kubernetes CronJob

Deploy as a scheduled job to run periodically:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: efs-purge
  namespace: default
spec:
  schedule: "0 2 * * *"  # Run daily at 2 AM
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: efspurge
            image: ghcr.io/alonalmog82/asyncefspurge:latest
            args:
              - /data
              - --max-age-days=30
              - --max-concurrency-scanning=1000
              - --max-concurrency-deletion=1000
              - --memory-limit-mb=800
              - --task-batch-size=5000
              - --remove-empty-dirs
              - --log-level=INFO
            volumeMounts:
            - name: efs-volume
              mountPath: /data
            resources:
              requests:
                memory: "256Mi"
                cpu: "500m"
              limits:
                memory: "512Mi"
                cpu: "1000m"
          volumes:
          - name: efs-volume
            persistentVolumeClaim:
              claimName: efs-pvc
```

Apply with:
```bash
kubectl apply -f k8s-cronjob.yaml
```

### Docker Compose

```yaml
version: '3.8'

services:
  efspurge:
    image: efspurge:latest
    volumes:
      - /mnt/efs:/data:ro  # Mount EFS read-only for safety
    command:
      - /data
      - --max-age-days=30
      - --dry-run
    environment:
      - PYTHONUNBUFFERED=1
```

### AWS ECS Task

Example ECS task definition:

```json
{
  "family": "efs-purge",
  "taskRoleArn": "arn:aws:iam::ACCOUNT:role/efs-purge-task-role",
  "executionRoleArn": "arn:aws:iam::ACCOUNT:role/efs-purge-execution-role",
  "networkMode": "awsvpc",
  "containerDefinitions": [
    {
      "name": "efspurge",
      "image": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/efspurge:latest",
      "command": [
        "/mnt/efs",
        "--max-age-days=30",
        "--max-concurrency-scanning=1000",
        "--max-concurrency-deletion=1000"
      ],
      "mountPoints": [
        {
          "sourceVolume": "efs",
          "containerPath": "/mnt/efs"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/efs-purge",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ],
  "volumes": [
    {
      "name": "efs",
      "efsVolumeConfiguration": {
        "fileSystemId": "fs-12345678",
        "transitEncryption": "ENABLED"
      }
    }
  ],
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024"
}
```

## Performance

Optimized for network filesystems with high latency:

- **Concurrent Directory Scanning**: Subdirectories processed in parallel (5-10x faster on deep hierarchies)
- **Batched Task Creation**: Prevents OOM on large directories
- **Memory Back-Pressure**: Automatic throttling when memory usage is high
- **Controlled Concurrency**: Prevents filesystem overload
- **Efficient I/O**: Async operations overlap network latency

### Benchmarks

Typical performance on AWS EFS (results vary based on file count and structure):

| File Count | Concurrency | Files/sec | Duration | Notes |
|------------|-------------|-----------|----------|-------|
| 100,000    | 1000        | ~1,500    | ~67s     | Shallow structure |
| 1,000,000  | 1000        | ~1,200    | ~14m     | Balanced tree |
| 10,000,000 | 2000        | ~1,500    | ~111m    | With v1.4.0 optimizations |

**See [PERFORMANCE.md](PERFORMANCE.md) for detailed tuning guide.**

## Output Format

Logs are JSON-formatted for easy parsing and integration with logging systems:

```json
{
  "timestamp": "2026-01-14 10:30:45,123",
  "level": "INFO",
  "message": "Purge operation completed",
  "logger": "efspurge",
  "extra_fields": {
    "files_scanned": 150000,
    "files_to_purge": 25000,
    "files_purged": 25000,
    "dirs_scanned": 5000,
    "empty_dirs_deleted": 125,
    "symlinks_skipped": 150,
    "errors": 0,
    "bytes_freed": 52428800000,
    "duration_seconds": 125.45,
    "files_per_second": 1195.42,
    "mb_freed": 50000.0
  }
}
```

## Safety Features

- **Dry Run Mode**: Preview operations without making changes (applies to both files and empty directories)
- **Symlink Handling**: Skips symbolic links to prevent accidental deletion
- **Root Directory Protection**: Root directory is never deleted, even if empty
- **Error Isolation**: Individual file errors don't stop the entire operation
- **Permission Handling**: Gracefully handles permission denied errors
- **Post-Order Deletion**: Empty directories deleted in safe order (children before parents)
- **Transparent Limitations**: See [Race Condition Considerations](#race-condition-considerations-toctou) for POSIX-inherent limitations that affect all file purgers

## Race Condition Considerations (TOCTOU)

### The Inherent POSIX Limitation

All file purgers—including this tool, `find -delete`, and custom bash scripts—face an inherent **Time-of-Check-Time-of-Use (TOCTOU)** race condition on POSIX systems:

```
1. stat() file → get mtime         ← Check
2. ... time passes ...             ← Race window  
3. if mtime < cutoff: remove()     ← Use (delete based on stale info)
```

Between checking the file's modification time and deleting it, the file could be:
- **Modified** (updating mtime, meaning it should no longer be deleted)
- **Replaced** (deleted and recreated with the same name)

**There is no atomic "delete-if-mtime-older-than-X" operation in POSIX.** This limitation affects every file cleanup tool.

### Risk Assessment

The **risk scales with the lag time** between detection and deletion:

| Scenario | Lag Time | Risk Level |
|----------|----------|------------|
| Sequential bash `find -delete` | Milliseconds per file | Very Low |
| This tool (small batches) | Milliseconds per file | Very Low |
| This tool (large batches, high concurrency) | Seconds to minutes | Low |
| Two-phase scan-then-delete scripts | Minutes to hours | Medium |

### Why We Don't Double-Stat

A common mitigation is to `stat()` the file twice—once during scanning and again immediately before deletion—to verify the mtime hasn't changed. We consciously chose **not** to implement this because:

1. **2× I/O overhead on network filesystems**: On AWS EFS with ~5ms latency per `stat()`, deleting 1 million files would add ~83 minutes of overhead (1M × 5ms × 2 extra stats).

2. **Doesn't eliminate the race**: The window shrinks from seconds to microseconds, but still exists between the second `stat()` and `remove()`.

3. **Use case fit**: This tool is designed for **temp files, caches, and logs** where:
   - Files are typically write-once or append-only
   - Occasional loss of a recently-modified file is acceptable
   - The cutoff age (e.g., 30 days) provides a large safety margin

4. **Operational alternatives exist**: If you need stronger guarantees, see mitigations below.

### Comparison: AsyncEFSPurge vs. Bash Scripts

| Aspect | AsyncEFSPurge | `find -mtime +30 -delete` |
|--------|---------------|---------------------------|
| **TOCTOU window** | Same (inherent to POSIX) | Same (inherent to POSIX) |
| **Throughput on EFS** | 1,000-2,000 files/sec | 10-50 files/sec |
| **Memory usage** | Bounded (streaming) | Minimal |
| **Concurrency** | Configurable (overlaps I/O) | Sequential |
| **Progress reporting** | Real-time JSON logs | None (or custom) |
| **Error handling** | Continues on errors, reports stats | Stops or silent |
| **Dry-run mode** | Built-in | Requires separate command |
| **Empty dir cleanup** | Built-in with rate limiting | Requires separate pass |

**When to use bash:**
- Simple one-off cleanup on local disk
- Very small file counts (< 10,000)
- Environments where installing Python isn't feasible

**When to use AsyncEFSPurge:**
- Network filesystems (EFS, NFS) with high latency
- Large file counts (100K+)
- Production workloads needing observability
- Automated/scheduled cleanup (CronJobs)

### Mitigations If You Need Stronger Guarantees

If your use case requires minimizing TOCTOU risk:

1. **Increase the age margin**: Use `--max-age-days 90` instead of `30`. Files 90 days old are far less likely to be actively modified.

2. **Schedule during quiet periods**: Run the purger during maintenance windows when no writes are expected.

3. **Use filesystem snapshots**: On EFS, take a snapshot, mount read-only, scan it, then delete from the live filesystem.

4. **Implement application-level coordination**: Have your application stop writing before the purge runs.

5. **Accept and monitor**: For most temp/cache use cases, the risk is acceptable. Monitor the `files_purged` metric and investigate anomalies.

### The Bottom Line

For temp file cleanup on network filesystems, the practical risk of TOCTOU is **extremely low**—a file would need to be modified in the exact milliseconds between stat and delete. The performance cost of double-stat on high-latency filesystems outweighs the marginal safety benefit.

If you're purging files where accidental deletion would be catastrophic, consider whether a purger is the right tool, or implement one of the mitigations above.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and contribution guidelines.

### Quick Dev Setup

```bash
# Clone repository
git clone https://github.com/alonalmog82/AsyncEFSPurge.git
cd AsyncEFSPurge

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linter
ruff check .

# Format code
ruff format .
```

## Configuration

### Environment Variables

- `PYTHONUNBUFFERED=1` - Recommended for real-time logging in containers
- `PYTHONDONTWRITEBYTECODE=1` - Prevents `.pyc` file creation
- `EFSPURGE_REMOVE_EMPTY_DIRS=1` - Enable empty directory removal (same as `--remove-empty-dirs` flag)
- `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE=N` - Maximum empty directories to delete per run (0 = unlimited, default: 500)
- `EFSPURGE_MAX_CONCURRENT_SUBDIRS=N` - Maximum subdirectories to scan concurrently (default: 100, lower for deep trees)
- `EFSPURGE_MAX_CONCURRENCY=N` - [DEPRECATED] Maximum concurrent operations (use `EFSPURGE_MAX_CONCURRENCY_SCANNING`/`EFSPURGE_MAX_CONCURRENCY_DELETION`)
- `EFSPURGE_MAX_CONCURRENCY_SCANNING=N` - Maximum concurrent file scanning operations (default: 1000)
- `EFSPURGE_MAX_CONCURRENCY_DELETION=N` - Maximum concurrent file deletion operations (default: 1000)

### Empty Directory Rate Limiting

The `--max-empty-dirs-to-delete` parameter controls the maximum number of empty directories to delete per run. **Default: 500 directories per run.**

**Why Rate Limiting?**
- **Avoid metadata storms**: On network filesystems like AWS EFS, deleting thousands of directories simultaneously can overwhelm the metadata service, causing performance degradation for other operations
- **Predictable behavior**: Limit sudden large-scale changes to your filesystem
- **Gradual cleanup**: Spread directory deletion across multiple CronJob runs for smoother operations
- **Reduced impact**: Your applications and other processes continue operating normally during cleanup

**Per-Run Design:**
Since this tool typically runs as a **CronJob**, the rate limit is per-execution (not per-time). Your CronJob schedule provides the time-based component:
- **Daily CronJob** with `--max-empty-dirs-to-delete=500` → **500 dirs/day max**
- **Hourly CronJob** with `--max-empty-dirs-to-delete=500` → **500 dirs/hour max**

**Common Scenarios:**

| Scenario | Recommended Setting | Environment Variable |
|----------|-------------------|---------------------|
| **Default (safe)** | 500 (default) | `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE=500` |
| **Initial cleanup** | 0 (unlimited) | `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE=0` |
| **Very conservative** | 100 | `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE=100` |
| **Aggressive** | 2000 | `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE=2000` |

**Example workflow:**
```bash
# First run: unlimited to clean up existing backlog
efspurge /data --max-age-days 30 --remove-empty-dirs --max-empty-dirs-to-delete 0

# Subsequent runs: use default (500) for safe, gradual cleanup
efspurge /data --max-age-days 30 --remove-empty-dirs
```

### Tuning Concurrency

The `--max-concurrency-scanning` and `--max-concurrency-deletion` parameters should be tuned based on your filesystem:

**Scanning (stat operations):**
- **Local disk**: 500-1000
- **Network filesystem (NFS/SMB)**: 1000-2000
- **AWS EFS**: 2000-3000 (higher is better due to high latency)
- **Object storage**: 3000-5000

**Deletion (remove operations):**
- **Local disk**: 500-1000
- **Network filesystem (NFS/SMB)**: 500-1000
- **AWS EFS**: 1000-2000 (may be slower than scanning)
- **Object storage**: 1000-2000

**Note:** `--max-concurrency` is deprecated but still works (sets both scanning and deletion to the same value). Use separate parameters for better control.

Start with defaults and increase if you're not saturating network/IOPS. See [CONCURRENCY_TUNING.md](CONCURRENCY_TUNING.md) for detailed guidance.

### Tuning Memory for Deep Directory Trees

The `--max-concurrent-subdirs` parameter controls how many subdirectories are scanned concurrently at each level of the directory tree. **Default: 100.**

**Why This Matters:**

On deep directory trees, concurrent subdirectory scanning creates a recursive explosion of coroutines:

```
Level 1: 100 concurrent scans
Level 2: 100 × 100 = 10,000 pending coroutines
Level 3: 100 × 100 × 100 = 1,000,000 pending coroutines
→ Memory explodes before any files are processed!
```

**When to Reduce This Value:**

- Pod getting OOM killed despite low `--max-concurrency`
- Memory usage spikes during directory traversal (before file processing)
- Deep directory hierarchies (many nested folders)
- Memory-constrained environments (small Kubernetes pods)

**Recommended Settings by Environment:**

| Environment | `--max-concurrent-subdirs` | Notes |
|-------------|---------------------------|-------|
| **Default** | 100 | Good for most use cases |
| **Memory-constrained (512Mi-1Gi pod)** | 10-20 | Prevents recursive explosion |
| **Very deep trees (10+ levels)** | 5-10 | Keeps memory bounded |
| **Large memory (4Gi+ pod)** | 100-200 | Can handle more parallelism |

**Example for memory-constrained environments:**
```bash
efspurge /data \
  --max-age-days 30 \
  --max-concurrency=100 \
  --task-batch-size=500 \
  --max-concurrent-subdirs=10 \
  --memory-limit-mb=400
```

**Environment Variable:** `EFSPURGE_MAX_CONCURRENT_SUBDIRS`

## Troubleshooting

### Permission Errors

```bash
# Ensure the container has proper permissions
docker run --rm -v /mnt/efs:/data:rw efspurge:latest /data --max-age-days 30
```

### Memory Issues / OOM Kills

**For deep directory trees (memory explodes during traversal):**

```bash
# Reduce concurrent subdirectory scanning
efspurge /data --max-age-days 30 \
  --max-concurrent-subdirs=10 \
  --max-concurrency-scanning=100 \
  --max-concurrency-deletion=100 \
  --task-batch-size=500
```

**For large flat directories (millions of files):**

```bash
# Reduce batch sizes and increase container memory
docker run --rm -m 2g -v /mnt/efs:/data efspurge:latest /data \
  --max-age-days 30 \
  --task-batch-size=1000 \
  --memory-limit-mb=1200
```

**Key insight:** If memory spikes with `dirs_scanned` but `files_scanned=0`, the problem is `--max-concurrent-subdirs` (directory traversal), not file processing.

### Slow Performance

1. Increase `--max-concurrency-scanning` (try 2000-3000 for EFS)
2. Increase `--max-concurrency-deletion` (try 1000-2000 for EFS)
3. Ensure network connectivity is good
4. Check filesystem IOPS limits (AWS EFS scales with size)
5. Use `--log-level WARNING` to reduce logging overhead

#### Directory Scanning Bottleneck

**Symptom:** Directory scanning rate plateaus around 200-400 dirs/sec even with high `--max-concurrent-subdirs` (e.g., 4000).

**Root Cause:** The `async_scandir` function uses Python's default ThreadPoolExecutor, which has a limited thread pool size (typically 32 threads). Even if you set `--max-concurrent-subdirs` to 4000, only ~32 `os.scandir()` calls can run concurrently due to this thread pool limitation.

**Current Limitation:**
- Default thread pool size: `min(32, os.cpu_count() + 4)` threads
- This limits concurrent directory scanning operations regardless of `--max-concurrent-subdirs` setting
- Typical maximum directory scanning rate: ~250-300 dirs/sec on EFS

**Workaround:** This is a fundamental limitation of Python's default thread pool executor. To increase directory scanning throughput beyond ~300 dirs/sec, you would need to:
1. Modify the code to use a custom ThreadPoolExecutor with more threads
2. Or accept that directory scanning is I/O-bound and network latency limits the practical rate

**Note:** This bottleneck only affects directory scanning (`dirs_scanned` rate). File processing (`files_scanned` rate) is not affected and can scale independently with `--max-concurrency-scanning`.

**To Widen This Bottleneck (Code Changes Required):**

To increase directory scanning throughput beyond the default thread pool limit, you would need to modify `src/efspurge/purger.py`:

1. **Create a custom ThreadPoolExecutor** in `AsyncEFSPurger.__init__()`:
   ```python
   from concurrent.futures import ThreadPoolExecutor
   
   # In __init__, add:
   self.scandir_executor = ThreadPoolExecutor(max_workers=200)  # Or higher
   ```

2. **Modify `async_scandir()` to use the custom executor**:
   ```python
   async def async_scandir(path: Path, executor: ThreadPoolExecutor):
       """Async wrapper for os.scandir with custom executor."""
       loop = asyncio.get_running_loop()
       def _scandir():
           with os.scandir(path) as entries:
               return list(entries)
       return await loop.run_in_executor(executor, _scandir)
   ```

3. **Pass the executor when calling `async_scandir()`**:
   ```python
   entries = await async_scandir(directory, self.scandir_executor)
   ```

**Recommended Thread Pool Size:**
- For high-concurrency setups (`--max-concurrent-subdirs >= 1000`): 200-500 threads
- For moderate setups: 100-200 threads
- **Warning:** Too many threads can cause context switching overhead - test and tune based on your workload

**Trade-offs:**
- ✅ Higher directory scanning throughput (potentially 2-5x improvement)
- ✅ Better utilization of `--max-concurrent-subdirs` parameter
- ⚠️ Increased memory usage (each thread uses ~8MB stack space)
- ⚠️ More context switching overhead (diminishing returns beyond ~500 threads)

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Author

**Alon Almog** - [alon.almog@rivery.io](mailto:alon.almog@rivery.io)

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## Changelog

### Version 1.9.0 (2026-01-XX)
- **BREAKING CHANGE**: Split concurrency parameters into separate scanning and deletion limits
  - `--max-concurrency` is deprecated (use `--max-concurrency-scanning` and `--max-concurrency-deletion`)
  - `EFSPURGE_MAX_CONCURRENCY` env var is deprecated (use `EFSPURGE_MAX_CONCURRENCY_SCANNING`/`EFSPURGE_MAX_CONCURRENCY_DELETION`)
  - New parameters: `--max-concurrency-scanning` and `--max-concurrency-deletion` for independent control
  - Deprecated parameters still work but show warnings
- **New Feature**: All configuration parameters now support environment variables
  - `EFSPURGE_MAX_AGE_DAYS`, `EFSPURGE_MEMORY_LIMIT_MB`, `EFSPURGE_TASK_BATCH_SIZE`, `EFSPURGE_LOG_LEVEL`
  - Makes Kubernetes ConfigMap/Secret management easier
- **Enhancement**: Enhanced concurrency metrics in progress logs
  - Separate tracking for scanning vs deletion concurrency utilization
  - Better visibility for tuning each phase independently
- See [CHANGELOG.md](CHANGELOG.md) for detailed changelog

### Version 1.7.3 (2026-01-17)
- **New Parameter**: `--max-concurrent-subdirs` to fix OOM on deep directory trees
  - Default: 100 (unchanged behavior)
  - Set to 10-20 for memory-constrained pods or very deep trees
- See [CHANGELOG.md](CHANGELOG.md) for detailed changelog

### Version 1.7.2 (2026-01-17)
- **Bug Fixes**: Python 3.10+ compatibility, exception logging, progress tracking
- **Safety**: Block system directories, track special files
- See [CHANGELOG.md](CHANGELOG.md) for detailed changelog

### Version 1.7.0 (2026-01-16)
- **New Feature**: Empty directory rate limiting (`--max-empty-dirs-to-delete`)
- Default: 500 directories per run to prevent metadata storms

### Version 1.6.0 (2026-01-16)
- **New Feature**: Empty directory removal (`--remove-empty-dirs` flag)
- Post-order deletion with cascading parent cleanup
- See [CHANGELOG.md](CHANGELOG.md) for detailed changelog

### Version 1.4.0 (2026-01-15)
- **Major performance improvements**:
  - Concurrent subdirectory scanning (5-10x faster on deep hierarchies)
  - Batched task creation to prevent OOM on large directories
  - Memory back-pressure with automatic throttling
- Add `--memory-limit-mb` parameter (default: 800)
- Add `--task-batch-size` parameter (default: 5000)
- Add memory usage monitoring and back-pressure events tracking
- New PERFORMANCE.md guide for tuning

### Version 1.0.0 (2026-01-14)
- Initial release
- Async file scanning and deletion
- Age-based filtering
- Docker support
- JSON logging
- Comprehensive statistics

