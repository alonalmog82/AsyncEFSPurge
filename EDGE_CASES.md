# 🚨 Edge Cases and Security Analysis

## Critical Edge Cases Identified

### 1. ⚠️ **Race Condition: File Deleted Between Stat and Remove**

**Location**: `process_file()` lines 197-206

**Issue**: 
```python
stat = await aiofiles.os.stat(file_path)  # File exists
# ... time passes ...
await aiofiles.os.remove(file_path)      # File might be gone!
```

**Risk**: `FileNotFoundError` is caught, but we already incremented `files_scanned`. Stats might be slightly off.

**Status**: ✅ **Handled** - FileNotFoundError is caught and logged as debug

**Recommendation**: Current handling is acceptable (non-critical stat inaccuracy)

---

### 2. ⚠️ **Symlink to Directory Not Fully Protected**

**Location**: `scan_directory()` line 278-282

**Issue**: We check `is_symlink` but what if:
- Symlink points to a directory?
- We skip it, but what if it's a symlink to a directory that contains old files?

**Risk**: **LOW** - By design, we don't follow symlinks (safety feature)

**Status**: ✅ **By Design** - Symlinks are intentionally skipped

**Recommendation**: Document this behavior clearly

---

### 3. ⚠️ **Path Traversal: Root Path Could Be Symlink**

**Location**: `purge()` line 402

**Issue**: If `root_path` itself is a symlink, we'll follow it. This could be dangerous if:
- Symlink points outside intended directory
- Symlink is changed during execution

**Risk**: **MEDIUM** - Could purge wrong directory

**Status**: ⚠️ **Not Protected** - `Path(root_path)` will resolve symlinks

**Recommendation**: Add explicit check to ensure root_path is not a symlink

---

### 4. ⚠️ **Concurrent File Creation During Scan**

**Location**: `scan_directory()` line 267

**Issue**: Files created AFTER `scandir()` completes won't be processed

**Risk**: **LOW** - Expected behavior (snapshot at scan time)

**Status**: ✅ **Expected** - This is normal filesystem behavior

---

### 5. ⚠️ **Memory Limit Edge Cases**

**Location**: `check_memory_pressure()` line 159

**Issues**:
- What if `memory_limit_mb = 0`? ✅ Handled (returns early)
- What if `memory_limit_mb < 0`? ⚠️ Not validated
- What if memory check fails? ⚠️ Could raise exception

**Risk**: **LOW** - Negative values unlikely, but should validate

**Status**: ⚠️ **Should Validate** - Add input validation

---

### 6. ⚠️ **Task Batch Size Edge Cases**

**Location**: `scan_directory()` line 289

**Issues**:
- What if `task_batch_size = 0`? ⚠️ Would process nothing!
- What if `task_batch_size = 1`? ⚠️ Very inefficient but works
- What if `task_batch_size < 0`? ⚠️ Would never trigger batch

**Risk**: **MEDIUM** - Zero batch size = no processing

**Status**: ⚠️ **Should Validate** - Add minimum batch size check

---

### 7. ⚠️ **Very Large Single Directory**

**Location**: `async_scandir()` line 30-38

**Issue**: `list(entries)` loads ALL entries into memory at once
- Directory with 1M files = 1M entries in memory
- Even with streaming, this could be a problem

**Risk**: **MEDIUM** - Could cause memory spike

**Status**: ⚠️ **Potential Issue** - Consider streaming scandir entries

**Recommendation**: For directories > 100K entries, consider chunked processing

---

### 8. ⚠️ **Concurrent Subdirectory Explosion**

**Location**: `scan_directory()` line 314

**Issue**: If a directory has 10,000 subdirectories, we spawn 10,000 concurrent tasks
- Each creates its own async tasks
- Memory could spike even with streaming

**Risk**: **MEDIUM** - Could overwhelm system

**Status**: ⚠️ **Should Limit** - Add max concurrent subdirs

**Recommendation**: Process subdirs in batches (e.g., max 100 concurrent)

---

### 9. ⚠️ **Background Task Cleanup**

**Location**: `purge()` lines 408-419

**Issue**: If `scan_directory()` raises exception, background task might not cancel properly

**Risk**: **LOW** - Finally block handles it

**Status**: ✅ **Handled** - Finally block ensures cleanup

---

### 10. ⚠️ **Time Zone Edge Cases**

**Location**: `__init__()` line 76

**Issue**: `time.time()` uses system timezone
- Files created in different timezone might have wrong mtime
- Daylight saving time transitions

**Risk**: **LOW** - EFS uses UTC, but local system might not

**Status**: ⚠️ **Documented** - Should document timezone behavior

---

### 11. ⚠️ **Special Files (Devices, FIFOs, Sockets)**

**Location**: `process_file()` line 197

**Issue**: What if entry is:
- Character/block device (`/dev/null`)
- FIFO (named pipe)
- Socket
- Other special file types

**Risk**: **LOW** - `aiofiles.os.stat()` will work, but `remove()` might fail

**Status**: ✅ **Handled** - Exception caught and logged

---

### 12. ⚠️ **Unicode and Long Paths**

**Location**: Throughout

**Issues**:
- Non-ASCII characters in paths
- Very long paths (> 260 chars on Windows, > 4096 on Linux)
- Paths with control characters

**Risk**: **LOW** - Python handles Unicode well, but should test

**Status**: ⚠️ **Should Test** - Add Unicode path tests

---

### 13. ⚠️ **Disk Full During Deletion**

**Location**: `process_file()` line 206

**Issue**: What if disk fills up during deletion?
- `remove()` might fail with `OSError: [Errno 28] No space left on device`

**Risk**: **LOW** - Exception caught, but partial deletion might occur

**Status**: ✅ **Handled** - Exception caught and logged

---

### 14. ⚠️ **Circular Directory Structure**

**Location**: `scan_directory()` line 314

**Issue**: What if there are hard links creating cycles?
- Actually impossible with directories (hard links to dirs not allowed)
- But symlinks could create cycles (we skip them)

**Risk**: **NONE** - Hard links to dirs impossible, symlinks skipped

**Status**: ✅ **Safe** - No risk

---

### 15. ⚠️ **Empty Directories**

**Location**: `scan_directory()` line 267

**Issue**: Empty directories are scanned but not removed
- This is intentional (only files are purged)
- But could leave empty directory structure

**Risk**: **NONE** - By design

**Status**: ✅ **By Design** - Only files are purged

---

## 🔒 Security Concerns

### 1. **Path Traversal Protection**

**Current**: None - relies on filesystem permissions

**Recommendation**: Add explicit check that root_path is absolute and not a symlink

### 2. **Input Validation**

**Missing**:
- `max_age_days` could be negative
- `max_concurrency` could be 0 or negative
- `task_batch_size` could be 0 or negative
- `memory_limit_mb` could be negative

**Recommendation**: Add validation in `__init__()`

### 3. **Resource Exhaustion**

**Risk**: Unbounded concurrent subdirectory processing

**Recommendation**: Limit concurrent subdirs (e.g., max 100)

---

## ✅ Recommendations Summary

### High Priority
1. ✅ **Add input validation** for all parameters
2. ✅ **Limit concurrent subdirectories** (prevent explosion)
3. ✅ **Add root path validation** (ensure not symlink, absolute path)

### Medium Priority
4. ⚠️ **Consider streaming scandir** for very large directories
5. ⚠️ **Add Unicode path tests**
6. ⚠️ **Document timezone behavior**

### Low Priority
7. ⚠️ **Add tests for special file types**
8. ⚠️ **Add tests for edge cases**

---

## 🧪 Test Coverage Gaps

Current tests only cover:
- ✅ Version check
- ✅ Import check
- ✅ Basic initialization

**Missing**:
- ❌ Actual file processing
- ❌ Directory scanning
- ❌ Memory back-pressure
- ❌ Error handling
- ❌ Edge cases
- ❌ Integration tests

