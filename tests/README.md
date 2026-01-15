# Tests directory for AsyncEFSPurge

This directory contains test files for the AsyncEFSPurge project.

## Running Tests

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with coverage
pytest --cov=efspurge --cov-report=html

# Run specific test file
pytest tests/test_basic.py
```

## Test Structure

- `test_basic.py` - Basic import and initialization tests
- Add more test files as needed following the `test_*.py` naming convention

