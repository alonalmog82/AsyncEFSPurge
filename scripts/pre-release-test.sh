#!/bin/bash
# Pre-release test script - run before tagging a release
# This runs comprehensive tests including integration tests

set -e

echo "🧪 Pre-Release Test Suite"
echo "=========================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ]; then
    echo -e "${RED}Error: Must run from project root${NC}"
    exit 1
fi

echo "📦 Installing dependencies..."
pip install -e ".[dev]" --quiet

echo ""
echo "🔍 Running linting..."
ruff check . || {
    echo -e "${RED}❌ Linting failed${NC}"
    exit 1
}
ruff format --check . || {
    echo -e "${RED}❌ Formatting check failed${NC}"
    exit 1
}
echo -e "${GREEN}✅ Linting passed${NC}"

echo ""
echo "🧪 Running unit tests..."
pytest tests/ -v -m "not integration" --cov=efspurge --cov-report=term-missing || {
    echo -e "${RED}❌ Unit tests failed${NC}"
    exit 1
}
echo -e "${GREEN}✅ Unit tests passed${NC}"

echo ""
echo "🔬 Running edge case tests..."
pytest tests/test_edge_cases.py -v || {
    echo -e "${RED}❌ Edge case tests failed${NC}"
    exit 1
}
echo -e "${GREEN}✅ Edge case tests passed${NC}"

echo ""
echo "🌐 Running integration tests..."
pytest tests/test_integration.py -v -m integration || {
    echo -e "${RED}❌ Integration tests failed${NC}"
    exit 1
}
echo -e "${GREEN}✅ Integration tests passed${NC}"

echo ""
echo "📊 Running streaming architecture test..."
if [ -f "scripts/test-streaming.sh" ]; then
    ./scripts/test-streaming.sh || {
        echo -e "${RED}❌ Streaming architecture test failed${NC}"
        exit 1
    }
    echo -e "${GREEN}✅ Streaming architecture test passed${NC}"
else
    echo -e "${YELLOW}⚠️  Streaming test script not found, skipping${NC}"
fi

echo ""
echo "=========================="
echo -e "${GREEN}✅ All pre-release tests passed!${NC}"
echo ""
echo "Ready to tag and release! 🚀"

