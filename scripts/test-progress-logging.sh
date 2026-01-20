#!/bin/bash
# Test script to verify progress logging works in dry-run mode

set -e

echo "🧪 Testing progress logging in dry-run mode..."
echo ""

# Create test directory with many files
TEST_DIR="/tmp/efspurge-progress-test"
rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR"

echo "Creating test files (this will take a moment)..."
# Create enough files to last > 30 seconds
for i in {1..5000}; do
    echo "test" > "$TEST_DIR/file-$i.txt"
done

echo "Created 5000 test files in $TEST_DIR"
echo ""
echo "Running efspurge with dry-run (watch for progress updates every 30s)..."
echo ""

# Run with low concurrency to make it slower and see progress
docker run --rm -v "$TEST_DIR:/data" \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data \
  --max-age-days 0 \
  --max-concurrency 50 \
  --dry-run \
  --log-level INFO

echo ""
echo "Cleaning up..."
rm -rf "$TEST_DIR"

echo ""
echo "✅ Test complete!"
echo ""
echo "Expected output:"
echo "  - 'Starting EFS purge - DRY RUN MODE'"
echo "  - 'Progress update' (every 30 seconds)"
echo "  - 'Purge operation completed'"

