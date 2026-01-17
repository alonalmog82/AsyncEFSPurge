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

# High concurrency for network storage
efspurge /mnt/efs --max-age-days 7 --max-concurrency 2000
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
  --max-concurrency N       Maximum concurrent async operations (default: 1000)
  --memory-limit-mb MB      Soft memory limit in MB, triggers back-pressure (default: 800)
  --task-batch-size N       Maximum tasks to create at once, prevents OOM (default: 5000)
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
efspurge /mnt/efs --max-age-days 7 --max-concurrency 2000 --memory-limit-mb 1600
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
              - --max-concurrency=1000
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
        "--max-concurrency=1000"
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

The `--max-concurrency` parameter should be tuned based on your filesystem:

- **Local disk**: 100-500
- **Network filesystem (NFS/SMB)**: 500-1000
- **AWS EFS**: 1000-2000 (higher is better due to high latency)
- **Object storage**: 2000-5000

Start with defaults and increase if you're not saturating network/IOPS.

## Troubleshooting

### Permission Errors

```bash
# Ensure the container has proper permissions
docker run --rm -v /mnt/efs:/data:rw efspurge:latest /data --max-age-days 30
```

### Memory Issues

For extremely large directories (millions of files), increase container memory:

```bash
docker run --rm -m 2g -v /mnt/efs:/data efspurge:latest /data --max-age-days 30
```

### Slow Performance

1. Increase `--max-concurrency` (try 2000-5000 for EFS)
2. Ensure network connectivity is good
3. Check filesystem IOPS limits (AWS EFS scales with size)
4. Use `--log-level WARNING` to reduce logging overhead

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Author

**Alon Almog** - [alon.almog@rivery.io](mailto:alon.almog@rivery.io)

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## Changelog

### Version 1.6.0 (2026-01-16)
- **New Feature**: Empty directory removal (`--remove-empty-dirs` flag)
  - Post-order deletion (children before parents)
  - Cascading deletion (parents checked after children deleted)
  - Root directory always preserved
  - Respects `--dry-run` mode
- **Critical Bug Fixes**:
  - Fixed race condition: Duplicate directory entries from concurrent scans
  - Fixed list modification during iteration
  - Fixed path comparison edge cases
  - Fixed cascading deletion logic
- **Improvements**:
  - Added `remove_empty_dirs` to startup log output
  - Comprehensive test coverage (40 tests passing)
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

