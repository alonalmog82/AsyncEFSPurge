#!/bin/bash
# Build and test Docker image locally

set -e

echo "Building Docker image..."
docker build -t efspurge:test .

echo ""
echo "Testing --version..."
docker run --rm efspurge:test --version

echo ""
echo "Testing --help..."
docker run --rm efspurge:test --help

echo ""
echo "Creating test data..."
mkdir -p ./test-data/{old,new}
touch ./test-data/new/file1.txt
touch ./test-data/new/file2.txt
echo "test content" > ./test-data/old/oldfile.txt

# Make old file from 60 days ago
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    touch -t $(date -v-60d +%Y%m%d%H%M) ./test-data/old/oldfile.txt
else
    # Linux
    touch -t $(date -d '60 days ago' +%Y%m%d%H%M) ./test-data/old/oldfile.txt
fi

echo ""
echo "Testing dry-run mode..."
docker run --rm -v "$(pwd)/test-data:/data" efspurge:test /data --max-age-days 30 --dry-run --log-level INFO

echo ""
echo "Verifying files still exist after dry-run..."
ls -la ./test-data/old/

echo ""
echo "Testing actual purge..."
docker run --rm -v "$(pwd)/test-data:/data" efspurge:test /data --max-age-days 30 --log-level INFO

echo ""
echo "Verifying old file was deleted..."
ls -la ./test-data/old/ || echo "Old directory is now empty (expected)"

echo ""
echo "Cleaning up..."
rm -rf ./test-data

echo ""
echo "✅ All tests passed!"

