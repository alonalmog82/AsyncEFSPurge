# 🗂️ Empty Directory Removal Feature

## Overview

Added support for removing empty directories after file purging operations. This feature uses **post-order deletion** to safely handle nested empty directory structures.

---

## 🎯 Design Decisions

### ✅ Implemented

1. **Post-Order Deletion**: Children deleted before parents
   - Ensures nested empty directories are handled correctly
   - Example: `/a/b/c` → deletes `c`, then `b`, then `a`

2. **Root Directory Protection**: Root directory is **never** deleted
   - Even if root becomes empty, it's preserved
   - Safety feature to prevent accidental data loss

3. **Immediate Detection**: Empty directories detected during scan
   - Checked after all subdirectories are processed
   - Only directories that are empty at scan time are considered

4. **Cascading Deletion**: After deleting a directory, parent is re-checked
   - If parent becomes empty, it's also deleted
   - Handles nested structures automatically

---

## 📋 Usage

### CLI Flag

```bash
efspurge /data --max-age-days 30 --remove-empty-dirs
```

### Environment Variable

```bash
# Set environment variable
export EFSPURGE_REMOVE_EMPTY_DIRS=1

# Or in Kubernetes
env:
  - name: EFSPURGE_REMOVE_EMPTY_DIRS
    value: "1"
```

### Kubernetes Example

```yaml
args:
  - /data
  - --max-age-days=30
  - --remove-empty-dirs  # Enable empty directory removal
```

---

## 🔍 How It Works

### Step-by-Step Process

1. **During Scan**:
   - Scan directory entries (files and subdirs)
   - Process files (existing logic)
   - Recursively process subdirectories
   - **After subdirs processed**: Check if directory is empty
   - If empty (and not root): Add to deletion list

2. **After Scan Completes**:
   - Sort directories by depth (deepest first)
   - Delete in post-order (children before parents)
   - After each deletion, check if parent is now empty
   - If parent empty: Add to deletion list and continue

### Example Flow

```
Initial structure:
/data/
  /a/
    /b/
      /c/  (empty)
    /b/    (empty after c deleted)
  /a/      (empty after b deleted)

Scan phase:
1. Scan /data/a/b/c → empty → add to list
2. Scan /data/a/b → has c → not empty
3. Scan /data/a → has b → not empty

Deletion phase:
1. Delete /data/a/b/c → check parent /data/a/b → now empty → add to list
2. Delete /data/a/b → check parent /data/a → now empty → add to list
3. Delete /data/a → check parent /data → root, skip
```

---

## 🧪 Test Coverage

### Test Cases

1. ✅ Empty dir not removed by default
2. ✅ Empty dir removed when enabled
3. ✅ Root directory never removed
4. ✅ Nested empty dirs (post-order)
5. ✅ Dir with files not removed
6. ✅ Dir with non-empty subdirs not removed
7. ✅ Dir with empty subdirs removed (cascading)
8. ✅ Dry-run reports empty dirs
9. ✅ Multiple empty dirs

---

## 📊 Statistics

New stat field added:
- `empty_dirs_deleted`: Number of empty directories removed

Example output:
```json
{
  "files_scanned": 1000,
  "files_purged": 500,
  "empty_dirs_deleted": 15,
  ...
}
```

---

## ⚠️ Safety Features

1. **Root Protection**: Root directory never deleted
2. **Double-Check**: Re-verifies directory is empty before deletion
3. **Race Condition Handling**: Handles directories populated/deleted by other processes
4. **Permission Errors**: Gracefully handles permission denied
5. **Dry-Run Support**: Reports what would be deleted without actually deleting

---

## 🔄 Edge Cases Handled

1. **Directory populated during scan**: Skipped (not empty anymore)
2. **Directory deleted by another process**: Handled gracefully
3. **Permission denied**: Logged as warning, continues
4. **Nested empty dirs**: Deleted in correct order (post-order)
5. **Root directory**: Always preserved

---

## 📝 Implementation Details

### Key Methods

- `_check_empty_directory()`: Checks if directory is empty after subdirs processed
- `_remove_empty_directories()`: Deletes empty directories in post-order

### Data Structures

- `self.empty_dirs`: List of empty directories to delete
- Sorted by depth (deepest first) for post-order deletion

---

## 🎯 Use Cases

### When to Use

✅ **Good for**:
- Cleaning up empty directory structures
- Removing orphaned empty directories
- Maintaining clean filesystem structure
- Post-cleanup maintenance

⚠️ **Be careful with**:
- Directories that might be populated by other processes
- Shared filesystems with concurrent access
- Directories used as mount points (will fail gracefully)

---

## 📚 Related Documentation

- [PRODUCTION_SAFETY.md](PRODUCTION_SAFETY.md) - Safety guidelines
- [TESTING.md](TESTING.md) - Test suite documentation
- [EDGE_CASES.md](EDGE_CASES.md) - Edge case analysis

---

## ✅ Status

**Feature Status**: ✅ **Production Ready**

- ✅ Implemented and tested
- ✅ 9 comprehensive tests
- ✅ All edge cases handled
- ✅ CLI and env var support
- ✅ Documentation complete

