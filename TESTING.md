# 🧪 Testing Guide

## Test Suite Overview

### Test Categories

1. **Unit Tests** (`test_basic.py`, `test_edge_cases.py`)
   - Fast, isolated tests
   - Test individual components
   - Run on every commit

2. **Integration Tests** (`test_integration.py`)
   - Test full workflows
   - Use real file system operations
   - Marked with `@pytest.mark.integration`
   - Run in CI and before releases

3. **Streaming Architecture Tests** (`scripts/test-streaming.sh`)
   - Large-scale performance tests
   - Verify memory efficiency
   - Run before releases

---

## Running Tests

### Run All Tests
```bash
pytest
```

### Run Only Unit Tests (Fast)
```bash
pytest -m "not integration"
```

### Run Only Integration Tests
```bash
pytest -m integration
```

### Run Edge Case Tests
```bash
pytest tests/test_edge_cases.py -v
```

### Run with Coverage
```bash
pytest --cov=efspurge --cov-report=html
```

### Run Pre-Release Test Suite
```bash
./scripts/pre-release-test.sh
```

---

## Test Coverage

### Current Coverage

| Component | Coverage | Status |
|-----------|----------|--------|
| Core Logic | ~60% | ✅ Good |
| Edge Cases | ~80% | ✅ Good |
| Integration | ~70% | ✅ Good |
| CLI | ~25% | ⚠️ Needs work |

### Coverage Goals

- **Minimum**: 60% overall
- **Target**: 80% overall
- **Critical paths**: 90%+

---

## Test Files

### `tests/test_basic.py`
- Version check
- Import tests
- Basic initialization

### `tests/test_edge_cases.py`
- File deletion race conditions
- Symlink handling
- Permission errors
- Empty directories
- Nested structures
- Batch size variations
- Dry-run vs actual deletion
- Memory limit edge cases

### `tests/test_integration.py`
- Large flat directories (1000+ files)
- Large nested structures (1000+ files)
- Actual deletion workflows
- Memory stress tests
- Streaming architecture verification
- Progress update verification

### `scripts/test-streaming.sh`
- Flat directory with 10K files
- Nested structure with 10K files
- Actual deletion test
- Memory stress test

---

## CI/CD Integration

### GitHub Actions

Tests run automatically on:
- Every push to `main` or `develop`
- Every pull request
- Unit tests: Fast (run always)
- Integration tests: Slower (run in CI)

### Pre-Release Checklist

Before tagging a release, run:
```bash
./scripts/pre-release-test.sh
```

This runs:
1. ✅ Linting (ruff check + format)
2. ✅ Unit tests
3. ✅ Edge case tests
4. ✅ Integration tests
5. ✅ Streaming architecture test

---

## Writing New Tests

### Unit Test Template
```python
@pytest.mark.asyncio
async def test_feature_name(temp_dir):
    """Test description."""
    # Setup
    test_file = temp_dir / "test.txt"
    test_file.write_text("content")
    
    # Execute
    purger = AsyncEFSPurger(...)
    await purger.scan_directory(temp_dir)
    
    # Assert
    assert purger.stats["files_scanned"] == 1
```

### Integration Test Template
```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_large_scale_feature(large_test_structure):
    """Test description."""
    # Use large_test_structure fixture
    # Test with realistic data volumes
    # Verify performance characteristics
```

---

## Known Test Limitations

1. **Windows Compatibility**: Some tests use Unix-specific features (chmod)
2. **Time-dependent**: Tests using `time.time()` may be flaky
3. **File System**: Tests create temporary files (cleaned up automatically)

---

## Debugging Failed Tests

### Run Single Test
```bash
pytest tests/test_edge_cases.py::test_specific_test -v
```

### Run with Output
```bash
pytest -v -s  # Show print statements
```

### Run with Debugger
```bash
pytest --pdb  # Drop into debugger on failure
```

---

## Test Maintenance

### When to Add Tests

- ✅ New feature added
- ✅ Bug fixed
- ✅ Edge case discovered
- ✅ Performance optimization

### Test Quality Checklist

- [ ] Test name describes what it tests
- [ ] Test is isolated (doesn't depend on other tests)
- [ ] Test cleans up after itself
- [ ] Test is deterministic (same result every run)
- [ ] Test covers the happy path AND edge cases

---

## Performance Benchmarks

Integration tests include performance checks:
- Memory usage should be < 200 MB for 1000 files
- Processing speed: 5K-15K files/sec
- Zero backpressure events under normal conditions

---

## Continuous Improvement

- Review test coverage regularly
- Add tests for production issues
- Keep tests fast (< 1 minute total)
- Document test assumptions

