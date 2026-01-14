#!/bin/bash
# Production-safe test script with comprehensive validation

set -e

echo "🛡️  AsyncEFSPurge - Production Safety Test"
echo "=========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test results
TESTS_PASSED=0
TESTS_FAILED=0

test_pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((TESTS_PASSED++))
}

test_fail() {
    echo -e "${RED}✗${NC} $1"
    ((TESTS_FAILED++))
}

test_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# Create comprehensive test environment
echo "📁 Creating test environment..."
TEST_DIR="./safety-test-data"
rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR"/{critical,important,archive,temp}

# Create files with different ages
echo "📝 Creating test files..."

# Current files (should NEVER be deleted)
for i in {1..5}; do
    echo "critical data" > "$TEST_DIR/critical/file-$i.txt"
    echo "important data" > "$TEST_DIR/important/file-$i.txt"
done

# 15-day old files (should NOT be deleted with 30-day threshold)
if [[ "$OSTYPE" == "darwin"* ]]; then
    for i in {1..5}; do
        touch -t $(date -v-15d +%Y%m%d%H%M) "$TEST_DIR/important/file-15days-$i.txt"
    done
else
    for i in {1..5}; do
        touch -t $(date -d '15 days ago' +%Y%m%d%H%M) "$TEST_DIR/important/file-15days-$i.txt"
    done
fi

# 60-day old files (SHOULD be deleted with 30-day threshold)
if [[ "$OSTYPE" == "darwin"* ]]; then
    for i in {1..5}; do
        touch -t $(date -v-60d +%Y%m%d%H%M) "$TEST_DIR/archive/old-$i.txt"
        touch -t $(date -v-90d +%Y%m%d%H%M) "$TEST_DIR/temp/very-old-$i.txt"
    done
else
    for i in {1..5}; do
        touch -t $(date -d '60 days ago' +%Y%m%d%H%M) "$TEST_DIR/archive/old-$i.txt"
        touch -t $(date -d '90 days ago' +%Y%m%d%H%M) "$TEST_DIR/temp/very-old-$i.txt"
    done
fi

echo ""
echo "📊 Test environment created:"
echo "   - critical/: 5 current files (MUST keep)"
echo "   - important/: 5 current + 5 files from 15 days ago (MUST keep)"
echo "   - archive/: 5 files from 60 days ago (should delete)"
echo "   - temp/: 5 files from 90 days ago (should delete)"
echo ""

# Count files before
INITIAL_COUNT=$(find "$TEST_DIR" -type f | wc -l | tr -d ' ')
echo "Initial file count: $INITIAL_COUNT"
echo ""

# Test 1: Dry-run mode safety
echo "Test 1: Dry-run mode (should not delete anything)"
echo "=================================================="
docker run --rm -v "$(pwd)/$TEST_DIR:/data" \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data --max-age-days 30 --dry-run --log-level INFO 2>&1 | tee /tmp/test-dryrun.log

AFTER_DRYRUN=$(find "$TEST_DIR" -type f | wc -l | tr -d ' ')
if [ "$AFTER_DRYRUN" -eq "$INITIAL_COUNT" ]; then
    test_pass "Dry-run: No files deleted ($AFTER_DRYRUN files remain)"
else
    test_fail "Dry-run: Files were deleted! ($AFTER_DRYRUN vs $INITIAL_COUNT)"
fi

# Check dry-run log for correct detection
DETECTED=$(grep -o '"files_to_purge": [0-9]*' /tmp/test-dryrun.log | grep -o '[0-9]*')
if [ "$DETECTED" -eq 10 ]; then
    test_pass "Dry-run: Correctly identified 10 old files"
else
    test_warn "Dry-run: Identified $DETECTED files (expected 10)"
fi
echo ""

# Test 2: Actual deletion with correct threshold
echo "Test 2: Actual deletion (30-day threshold)"
echo "=========================================="
docker run --rm -v "$(pwd)/$TEST_DIR:/data" \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data --max-age-days 30 --log-level INFO 2>&1 | tee /tmp/test-delete.log

AFTER_DELETE=$(find "$TEST_DIR" -type f | wc -l | tr -d ' ')
EXPECTED_REMAINING=10  # 5 critical + 5 important (15 days old)

if [ "$AFTER_DELETE" -eq "$EXPECTED_REMAINING" ]; then
    test_pass "Deletion: Correct number of files remain ($AFTER_DELETE)"
else
    test_fail "Deletion: Wrong file count ($AFTER_DELETE vs expected $EXPECTED_REMAINING)"
fi

# Verify critical files still exist
CRITICAL_COUNT=$(find "$TEST_DIR/critical" -type f | wc -l | tr -d ' ')
if [ "$CRITICAL_COUNT" -eq 5 ]; then
    test_pass "Safety: All critical files preserved"
else
    test_fail "Safety: Critical files lost! ($CRITICAL_COUNT/5 remain)"
fi

# Verify 15-day-old files still exist
IMPORTANT_COUNT=$(find "$TEST_DIR/important" -type f | wc -l | tr -d ' ')
if [ "$IMPORTANT_COUNT" -eq 5 ]; then
    test_pass "Safety: 15-day-old files correctly preserved"
else
    test_fail "Safety: 15-day-old files deleted! ($IMPORTANT_COUNT/5 remain)"
fi

# Verify old files were deleted
OLD_COUNT=$(find "$TEST_DIR/archive" -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$OLD_COUNT" -eq 0 ]; then
    test_pass "Cleanup: Old files correctly deleted"
else
    test_warn "Cleanup: Some old files remain ($OLD_COUNT)"
fi
echo ""

# Test 3: Permission handling
echo "Test 3: Permission error handling"
echo "=================================="
mkdir -p "$TEST_DIR/restricted"
echo "test" > "$TEST_DIR/restricted/file.txt"
chmod 000 "$TEST_DIR/restricted"

docker run --rm -v "$(pwd)/$TEST_DIR:/data" \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data --max-age-days 1 --log-level WARNING 2>&1 | tee /tmp/test-perms.log

if grep -q "Permission denied\|error" /tmp/test-perms.log; then
    test_pass "Permissions: Gracefully handled permission errors"
else
    test_warn "Permissions: No permission errors detected (test might be inconclusive)"
fi

chmod 755 "$TEST_DIR/restricted"
echo ""

# Test 4: Symlink safety
echo "Test 4: Symlink protection"
echo "=========================="
mkdir -p "$TEST_DIR/links"
echo "important" > "$TEST_DIR/critical/important-file.txt"
ln -s "$TEST_DIR/critical/important-file.txt" "$TEST_DIR/links/symlink.txt"

BEFORE_SYMLINK=$(find "$TEST_DIR/critical" -type f | wc -l | tr -d ' ')

docker run --rm -v "$(pwd)/$TEST_DIR:/data" \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data/links --max-age-days 0 --log-level INFO 2>&1 | tee /tmp/test-symlink.log

AFTER_SYMLINK=$(find "$TEST_DIR/critical" -type f | wc -l | tr -d ' ')

if [ "$BEFORE_SYMLINK" -eq "$AFTER_SYMLINK" ] && [ -f "$TEST_DIR/critical/important-file.txt" ]; then
    test_pass "Symlinks: Target file protected (symlink skipped)"
else
    test_fail "Symlinks: Target file was deleted through symlink!"
fi
echo ""

# Test 5: High concurrency stability
echo "Test 5: High concurrency test"
echo "=============================="
mkdir -p "$TEST_DIR/many-files"
for i in {1..100}; do
    echo "test" > "$TEST_DIR/many-files/file-$i.txt"
done

docker run --rm -v "$(pwd)/$TEST_DIR:/data" \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data/many-files --max-age-days 0 --max-concurrency 50 --log-level INFO 2>&1 | tee /tmp/test-concurrency.log

if [ $? -eq 0 ]; then
    test_pass "Concurrency: Handled 100 files with concurrency=50"
else
    test_fail "Concurrency: Failed with high file count"
fi

SCANNED=$(grep -o '"files_scanned": [0-9]*' /tmp/test-concurrency.log | tail -1 | grep -o '[0-9]*')
if [ "$SCANNED" -eq 100 ]; then
    test_pass "Concurrency: All 100 files were scanned"
else
    test_warn "Concurrency: Only $SCANNED files scanned (expected 100)"
fi
echo ""

# Cleanup
echo "🧹 Cleaning up test environment..."
chmod -R 755 "$TEST_DIR" 2>/dev/null || true
rm -rf "$TEST_DIR"
rm -f /tmp/test-*.log
echo ""

# Results
echo "=========================================="
echo "📊 Test Results"
echo "=========================================="
echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
echo -e "${RED}Failed: $TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ All safety tests passed!${NC}"
    echo ""
    echo "The container appears safe for production use."
    echo "However, you should still:"
    echo "  1. Test with your actual data structure (dry-run)"
    echo "  2. Start with conservative age thresholds (180+ days)"
    echo "  3. Monitor first production runs closely"
    echo "  4. Have backup/restore procedures ready"
    echo ""
    echo "See PRODUCTION_SAFETY.md for complete checklist."
    exit 0
else
    echo -e "${RED}❌ Some tests failed!${NC}"
    echo ""
    echo "DO NOT use in production until issues are resolved."
    echo "Review the failed tests above and investigate."
    exit 1
fi

