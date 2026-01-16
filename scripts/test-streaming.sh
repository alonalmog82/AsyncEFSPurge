#!/bin/bash
# Test script for v1.5.0 streaming architecture

set -e

echo "🧪 Testing v1.5.0 Streaming Architecture"
echo "========================================"
echo ""

# Create test environment
TEST_DIR="/tmp/efspurge-streaming-test"
rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR"

echo "📁 Creating test data..."
echo "  - Creating flat directory with 10,000 files..."
mkdir -p "$TEST_DIR/flat"
for i in {1..10000}; do
    touch "$TEST_DIR/flat/file-$i.txt"
done

echo "  - Creating nested directory structure..."
mkdir -p "$TEST_DIR/nested"
for dir in {1..100}; do
    mkdir -p "$TEST_DIR/nested/dir-$dir"
    for file in {1..100}; do
        touch "$TEST_DIR/nested/dir-$dir/file-$file.txt"
    done
done

echo "  - Making some files old (> 30 days)..."
touch -t 202301010000 "$TEST_DIR/flat"/file-{1..5000}.txt
touch -t 202301010000 "$TEST_DIR/nested"/dir-{1..50}/file-*.txt

echo ""
echo "📊 Test Data Summary:"
find "$TEST_DIR" -type f | wc -l | xargs echo "  Total files:"
find "$TEST_DIR" -type d | wc -l | xargs echo "  Total directories:"
echo ""

echo "=========================================="
echo "TEST 1: Dry Run on Flat Directory (10K files)"
echo "=========================================="
echo "Expected: Should process with low memory, no OOM"
echo ""

efspurge "$TEST_DIR/flat" \
    --max-age-days 30 \
    --max-concurrency 1000 \
    --memory-limit-mb 200 \
    --task-batch-size 2000 \
    --dry-run \
    --log-level INFO

echo ""
echo "=========================================="
echo "TEST 2: Dry Run on Nested Structure (10K files)"
echo "=========================================="
echo "Expected: Should handle concurrent subdirs, low memory"
echo ""

efspurge "$TEST_DIR/nested" \
    --max-age-days 30 \
    --max-concurrency 1000 \
    --memory-limit-mb 200 \
    --task-batch-size 2000 \
    --dry-run \
    --log-level INFO

echo ""
echo "=========================================="
echo "TEST 3: Actual Deletion (Flat)"
echo "=========================================="
echo "Expected: Should delete ~5000 old files"
echo ""

BEFORE=$(find "$TEST_DIR/flat" -type f | wc -l)
echo "Files before: $BEFORE"

efspurge "$TEST_DIR/flat" \
    --max-age-days 30 \
    --max-concurrency 1000 \
    --memory-limit-mb 200 \
    --task-batch-size 2000 \
    --log-level INFO

AFTER=$(find "$TEST_DIR/flat" -type f | wc -l)
echo "Files after: $AFTER"
echo "Deleted: $((BEFORE - AFTER))"

echo ""
echo "=========================================="
echo "TEST 4: Memory Stress Test (Low Limit)"
echo "=========================================="
echo "Expected: Some backpressure events, but should complete successfully"
echo ""

efspurge "$TEST_DIR/nested" \
    --max-age-days 30 \
    --max-concurrency 1000 \
    --memory-limit-mb 100 \
    --task-batch-size 1000 \
    --dry-run \
    --log-level INFO

echo ""
echo "=========================================="
echo "✅ All Tests Complete!"
echo "=========================================="
echo ""
echo "Check the output above for:"
echo "  ✓ Progress updates every 30 seconds"
echo "  ✓ Low memory usage (< 200 MB for most tests)"
echo "  ✓ Few or no backpressure events in TEST 1-3"
echo "  ✓ Correct file deletion in TEST 3"
echo ""
echo "Cleanup: rm -rf $TEST_DIR"

