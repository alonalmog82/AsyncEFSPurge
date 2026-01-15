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
  --max-age-days DAYS   Files older than this (in days) will be purged (default: 30.0)
  --max-concurrency N   Maximum concurrent async operations (default: 1000)
  --memory-limit-mb MB  Soft memory limit in MB, triggers back-pressure (default: 800)
  --task-batch-size N   Maximum tasks to create at once, prevents OOM (default: 5000)
  --dry-run            Don't actually delete files, just report what would be deleted
  --log-level LEVEL    Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
  --version            Show version and exit
  -h, --help           Show this help message and exit
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

- **Dry Run Mode**: Preview operations without making changes
- **Symlink Handling**: Skips symbolic links to prevent accidental deletion
- **Error Isolation**: Individual file errors don't stop the entire operation
- **Permission Handling**: Gracefully handles permission denied errors
- **Atomic Operations**: Uses filesystem-level operations for safety

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

