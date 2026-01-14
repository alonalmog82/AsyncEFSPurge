#!/bin/bash
# Quick start script to publish your first release to GHCR

set -e

echo "🚀 AsyncEFSPurge - Publishing to GitHub Container Registry"
echo ""

# Check if git is initialized
if [ ! -d .git ]; then
    echo "❌ Git repository not initialized"
    echo "Run: git init"
    exit 1
fi

# Check if GitHub CLI is installed
if ! command -v gh &> /dev/null; then
    echo "⚠️  GitHub CLI not found. You'll need to create the repo manually."
    echo "Install: brew install gh"
    echo ""
    USE_GH_CLI=false
else
    USE_GH_CLI=true
fi

# Check if remote exists
if ! git remote get-url origin &> /dev/null; then
    echo "📦 Creating GitHub repository..."
    
    if [ "$USE_GH_CLI" = true ]; then
        gh auth status || gh auth login
        gh repo create AsyncEFSPurge --public --source=. --remote=origin --push
        echo "✅ Repository created and pushed!"
    else
        echo ""
        echo "Please create repository manually:"
        echo "1. Go to https://github.com/new"
        echo "2. Name it 'AsyncEFSPurge'"
        echo "3. Make it public"
        echo "4. Don't initialize with README"
        echo "5. Create repository"
        echo ""
        echo "Then run these commands:"
        echo "  git remote add origin https://github.com/alonalmog82/AsyncEFSPurge.git"
        echo "  git branch -M main"
        echo "  git push -u origin main"
        exit 1
    fi
else
    echo "✅ Git remote already configured"
fi

# Check if we're on main branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "⚠️  Not on main branch (currently on: $CURRENT_BRANCH)"
    read -p "Switch to main? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git checkout -b main 2>/dev/null || git checkout main
    fi
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    echo "📝 You have uncommitted changes"
    read -p "Commit all changes? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git add .
        read -p "Enter commit message: " COMMIT_MSG
        git commit -m "${COMMIT_MSG:-"feat: ready for first publish"}"
    else
        echo "Please commit your changes first"
        exit 1
    fi
fi

# Push to main
echo ""
echo "📤 Pushing to main branch..."
git push origin main

echo ""
echo "🏷️  Creating version tag v1.0.0..."
if git rev-parse v1.0.0 >/dev/null 2>&1; then
    echo "⚠️  Tag v1.0.0 already exists"
    read -p "Create new version? Enter version (e.g., 1.0.1): " NEW_VERSION
    if [ -z "$NEW_VERSION" ]; then
        echo "Skipping tag creation"
    else
        git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION}"
        git push origin "v${NEW_VERSION}"
    fi
else
    git tag -a v1.0.0 -m "Release v1.0.0 - Initial release"
    git push origin v1.0.0
fi

echo ""
echo "✅ Done! GitHub Actions is now building and publishing your image."
echo ""
echo "📋 Next steps:"
echo ""
echo "1. Watch the build:"
echo "   https://github.com/alonalmog82/AsyncEFSPurge/actions"
echo ""
echo "2. After build completes, make package public:"
echo "   https://github.com/alonalmog82?tab=packages"
echo "   → Click 'asyncefspurge'"
echo "   → Package settings → Change visibility → Public"
echo ""
echo "3. Test your image:"
echo "   docker pull ghcr.io/alonalmog82/asyncefspurge:latest"
echo "   docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version"
echo ""
echo "🎉 Your image will be available at:"
echo "   ghcr.io/alonalmog82/asyncefspurge:latest"
echo "   ghcr.io/alonalmog82/asyncefspurge:1.0.0"
echo ""

