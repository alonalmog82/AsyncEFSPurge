# Contributing to AsyncEFSPurge

Thank you for your interest in contributing! This document provides guidelines and instructions for development.

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Docker (for container testing)
- Git

### Local Development Environment

1. **Clone the repository**

```bash
git clone https://github.com/yourusername/AsyncEFSPurge.git
cd AsyncEFSPurge
```

2. **Create a virtual environment**

```bash
# Using venv
python3.11 -m venv .venv

# Activate on Linux/macOS
source .venv/bin/activate

# Activate on Windows
.venv\Scripts\activate
```

3. **Install dependencies**

```bash
# Install package in editable mode with dev dependencies
pip install -e ".[dev]"
```

4. **Verify installation**

```bash
efspurge --version
pytest --version
ruff --version
```

## Development Workflow

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=efspurge --cov-report=html

# Run specific test file
pytest tests/test_purger.py

# Run with verbose output
pytest -v

# Run tests matching a pattern
pytest -k "test_dry_run"
```

### Code Quality

**Linting and Formatting:**

```bash
# Check code style
ruff check .

# Auto-fix issues where possible
ruff check --fix .

# Format code
ruff format .

# Check formatting without modifying
ruff format --check .
```

**Type Checking (Optional):**

```bash
# Install mypy
pip install mypy types-aiofiles

# Run type checker
mypy src/efspurge
```

### Running the Application

**From source:**

```bash
# Dry run on a test directory
efspurge /tmp/test --max-age-days 30 --dry-run --log-level DEBUG

# Create test data
mkdir -p /tmp/test-efs/{dir1,dir2,dir3}
for i in {1..100}; do touch /tmp/test-efs/dir1/file$i.txt; done

# Run purger
efspurge /tmp/test-efs --max-age-days 0 --dry-run
```

**In a Python script:**

```python
import asyncio
from efspurge.purger import AsyncEFSPurger

async def main():
    purger = AsyncEFSPurger(
        root_path="/tmp/test",
        max_age_days=30,
        max_concurrency=100,
        dry_run=True,
        log_level="DEBUG"
    )
    stats = await purger.purge()
    print(stats)

if __name__ == "__main__":
    asyncio.run(main())
```

## Docker Development

### Building the Image

```bash
# Build locally
docker build -t efspurge:dev .

# Build with specific Python version
docker build --build-arg PYTHON_VERSION=3.12 -t efspurge:dev .

# Build without cache
docker build --no-cache -t efspurge:dev .
```

### Testing the Docker Image

```bash
# Test help command
docker run --rm efspurge:dev --help

# Test with mounted volume
mkdir -p /tmp/test-data
docker run --rm -v /tmp/test-data:/data efspurge:dev \
  /data --max-age-days 30 --dry-run

# Test with environment variables
docker run --rm \
  -e PYTHONUNBUFFERED=1 \
  -v /tmp/test-data:/data \
  efspurge:dev /data --max-age-days 7 --dry-run
```

### Debugging Docker Container

```bash
# Run interactive shell
docker run --rm -it \
  --entrypoint /bin/bash \
  -v /tmp/test-data:/data \
  efspurge:dev

# Inside container, test manually
efspurge /data --max-age-days 30 --dry-run --log-level DEBUG
```

## Project Structure

```
AsyncEFSPurge/
├── .github/
│   └── workflows/          # CI/CD workflows
│       ├── ci.yml         # Testing and linting
│       └── docker.yml     # Docker image building
├── src/
│   └── efspurge/
│       ├── __init__.py    # Package version
│       ├── cli.py         # Command-line interface
│       ├── logging.py     # JSON logging utilities
│       └── purger.py      # Core purging logic
├── tests/                 # Test files (mirror src structure)
├── .dockerignore         # Docker build exclusions
├── Dockerfile            # Container definition
├── pyproject.toml        # Project metadata and dependencies
├── README.md             # User documentation
└── CONTRIBUTING.md       # This file
```

## Writing Tests

### Test Structure

```python
import pytest
from pathlib import Path
from efspurge.purger import AsyncEFSPurger

@pytest.mark.asyncio
async def test_purge_old_files(tmp_path):
    """Test that old files are identified for purging."""
    # Setup
    test_file = tmp_path / "old_file.txt"
    test_file.touch()
    
    # Make file old by modifying mtime
    import time
    old_time = time.time() - (60 * 86400)  # 60 days ago
    import os
    os.utime(test_file, (old_time, old_time))
    
    # Execute
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=30,
        dry_run=True
    )
    stats = await purger.purge()
    
    # Assert
    assert stats["files_scanned"] == 1
    assert stats["files_to_purge"] == 1
```

### Test Fixtures

```python
@pytest.fixture
def test_directory(tmp_path):
    """Create a test directory structure."""
    dirs = ["dir1", "dir2", "dir3"]
    for d in dirs:
        (tmp_path / d).mkdir()
        for i in range(10):
            (tmp_path / d / f"file{i}.txt").touch()
    return tmp_path

@pytest.mark.asyncio
async def test_with_fixture(test_directory):
    purger = AsyncEFSPurger(
        root_path=str(test_directory),
        max_age_days=30,
        dry_run=True
    )
    stats = await purger.purge()
    assert stats["files_scanned"] == 30
```

## Code Style Guide

### Python Style

- Follow PEP 8
- Use type hints for function signatures
- Maximum line length: 120 characters
- Use docstrings for modules, classes, and functions

**Example:**

```python
async def process_file(self, file_path: Path) -> None:
    """
    Process a single file - check age and purge if necessary.

    Args:
        file_path: Path to the file to process
    
    Raises:
        PermissionError: If file cannot be accessed
    """
    async with self.semaphore:
        # Implementation
        pass
```

### Async Best Practices

- Use `async/await` consistently
- Prefer `asyncio.gather()` for parallel operations
- Use `asyncio.Semaphore` for concurrency control
- Handle exceptions in async contexts properly

### Error Handling

```python
try:
    # Operation
    await aiofiles.os.remove(file_path)
except FileNotFoundError:
    # Specific exception - not an error in this context
    self.logger.debug(f"File already deleted: {file_path}")
except PermissionError as e:
    # Log with context
    log_with_context(
        self.logger,
        "warning",
        "Permission denied",
        {"file": str(file_path), "error": str(e)}
    )
except Exception as e:
    # Catch-all for unexpected errors
    log_with_context(
        self.logger,
        "error",
        "Unexpected error",
        {"file": str(file_path), "error_type": type(e).__name__}
    )
```

## Submitting Changes

### Pull Request Process

1. **Create a branch**

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/bug-description
```

2. **Make your changes**

- Write code following style guidelines
- Add tests for new functionality
- Update documentation as needed

3. **Test your changes**

```bash
# Run tests
pytest

# Check code quality
ruff check .
ruff format .

# Test Docker build
docker build -t efspurge:test .
```

4. **Commit your changes**

```bash
git add .
git commit -m "feat: add new feature"

# Use conventional commits:
# feat: new feature
# fix: bug fix
# docs: documentation changes
# test: test additions/changes
# refactor: code refactoring
# chore: maintenance tasks
```

5. **Push and create PR**

```bash
git push origin feature/your-feature-name
```

Then create a Pull Request on GitHub with:
- Clear description of changes
- Reference to any related issues
- Screenshots/logs if applicable

### PR Checklist

- [ ] Tests pass locally
- [ ] Code follows style guidelines
- [ ] New tests added for new functionality
- [ ] Documentation updated
- [ ] Commit messages are clear
- [ ] No merge conflicts

## Testing Locally Before PR

### Complete Pre-PR Check

```bash
# 1. Run all tests
pytest --cov=efspurge

# 2. Check code style
ruff check .
ruff format --check .

# 3. Build Docker image
docker build -t efspurge:test .

# 4. Test Docker image
docker run --rm efspurge:test --version
docker run --rm efspurge:test --help

# 5. Integration test
mkdir -p /tmp/pr-test
efspurge /tmp/pr-test --max-age-days 30 --dry-run
```

## Performance Testing

### Benchmarking

```python
import time
import asyncio
from efspurge.purger import AsyncEFSPurger

async def benchmark():
    start = time.time()
    
    purger = AsyncEFSPurger(
        root_path="/mnt/test",
        max_age_days=30,
        max_concurrency=1000,
        dry_run=True
    )
    
    stats = await purger.purge()
    duration = time.time() - start
    
    print(f"Files/sec: {stats['files_scanned'] / duration:.2f}")
    print(f"Total time: {duration:.2f}s")

asyncio.run(benchmark())
```

### Profile Performance

```bash
# Install profiling tools
pip install py-spy

# Profile running application
py-spy record -o profile.svg -- efspurge /mnt/test --max-age-days 30 --dry-run
```

## Release Process

### Version Bumping

1. Update version in `src/efspurge/__init__.py`
2. Update version in `pyproject.toml`
3. Update CHANGELOG (in README.md)
4. Commit: `git commit -m "chore: bump version to X.Y.Z"`
5. Tag: `git tag -a vX.Y.Z -m "Release X.Y.Z"`
6. Push: `git push origin main --tags`

GitHub Actions will automatically build and push Docker images.

## Getting Help

- **Issues**: Open an issue on GitHub
- **Discussions**: Use GitHub Discussions for questions
- **Email**: alon.almog@rivery.io

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

