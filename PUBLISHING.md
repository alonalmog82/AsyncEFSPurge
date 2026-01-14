# Publishing to GitHub Container Registry (GHCR)

This guide walks you through publishing your AsyncEFSPurge Docker images to GitHub Container Registry.

## ✅ Prerequisites

- GitHub repository created: https://github.com/alonalmog82/AsyncEFSPurge
- Code pushed to GitHub (see Initial Setup below)
- GitHub Actions enabled (enabled by default)

## 🚀 Initial Setup

### 1. Initialize Git Repository (if not done)

```bash
cd /Users/alonalmog/git/AsyncEFSPurge

# Initialize git
git init

# Add all files
git add .

# First commit
git commit -m "Initial commit: AsyncEFSPurge with CI/CD"
```

### 2. Create GitHub Repository

Option A - Using GitHub CLI (recommended):
```bash
# Install GitHub CLI if not installed
# macOS: brew install gh

# Login to GitHub
gh auth login

# Create repository
gh repo create AsyncEFSPurge --public --source=. --remote=origin --push

# Done! Your code is now on GitHub
```

Option B - Manually:
```bash
# 1. Go to https://github.com/new
# 2. Create repository named "AsyncEFSPurge"
# 3. Don't initialize with README (you already have one)
# 4. Create repository

# 5. Add remote and push
git remote add origin https://github.com/alonalmog82/AsyncEFSPurge.git
git branch -M main
git push -u origin main
```

### 3. Enable GitHub Packages (GHCR)

**Good news**: It's automatically enabled! No configuration needed.

The workflow uses `GITHUB_TOKEN` which is automatically provided by GitHub Actions.

### 4. Make Package Public (Optional but Recommended)

After your first publish:

1. Go to your GitHub profile packages: https://github.com/alonalmog82?tab=packages
2. Find `asyncefspurge` package
3. Click on it
4. Click "Package settings" (right sidebar)
5. Scroll down to "Danger Zone"
6. Click "Change visibility" → "Public"
7. Confirm

This allows anyone to pull your images without authentication.

## 📦 Publishing Your First Release

### Method 1: Push to Main (Latest Tag)

```bash
# Make sure all changes are committed
git add .
git commit -m "feat: ready for first publish"
git push origin main

# This will:
# ✅ Run CI tests
# ✅ Build Docker image for amd64 + arm64
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:latest
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:main
```

### Method 2: Create a Version Release (Recommended)

```bash
# Create and push a version tag
git tag -a v1.0.0 -m "Release v1.0.0 - Initial release"
git push origin main --tags

# This will:
# ✅ Run CI tests
# ✅ Build Docker image for amd64 + arm64
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:1.0.0
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:1.0
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:1
# ✅ Push to ghcr.io/alonalmog82/asyncefspurge:latest
```

## 🔍 Monitoring the Build

### Watch GitHub Actions

```bash
# Open actions in browser
gh workflow view Docker --web

# Or manually:
# https://github.com/alonalmog82/AsyncEFSPurge/actions
```

### Check Build Status

```bash
# List workflow runs
gh run list --workflow=Docker

# View specific run details
gh run view <run-id>

# Watch a run in progress
gh run watch
```

## 📥 Using Your Published Image

### Pull and Test

```bash
# Pull your published image
docker pull ghcr.io/alonalmog82/asyncefspurge:latest

# Verify it works
docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version

# Test with data
docker run --rm -v ~/test-data:/data \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --max-age-days 30 --dry-run
```

### Available Tags

After releasing v1.0.0, you'll have:

```bash
ghcr.io/alonalmog82/asyncefspurge:latest    # Always points to latest release
ghcr.io/alonalmog82/asyncefspurge:main      # Latest from main branch
ghcr.io/alonalmog82/asyncefspurge:1.0.0     # Specific version
ghcr.io/alonalmog82/asyncefspurge:1.0       # Minor version
ghcr.io/alonalmog82/asyncefspurge:1         # Major version
ghcr.io/alonalmog82/asyncefspurge:sha-abc123 # Specific commit
```

## 🔄 Release Workflow

### For New Features (Minor Release)

```bash
# 1. Update version
echo '__version__ = "1.1.0"' > src/efspurge/__init__.py
sed -i '' 's/version = "1.0.0"/version = "1.1.0"/' pyproject.toml

# 2. Commit changes
git add .
git commit -m "feat: add new feature - bump to v1.1.0"

# 3. Tag and push
git tag -a v1.1.0 -m "Release v1.1.0 - New features"
git push origin main --tags
```

### For Bug Fixes (Patch Release)

```bash
# 1. Update version
echo '__version__ = "1.0.1"' > src/efspurge/__init__.py
sed -i '' 's/version = "1.0.0"/version = "1.0.1"/' pyproject.toml

# 2. Commit changes
git add .
git commit -m "fix: important bug fix - bump to v1.0.1"

# 3. Tag and push
git tag -a v1.0.1 -m "Release v1.0.1 - Bug fixes"
git push origin main --tags
```

## 🔐 Security

### Vulnerability Scanning

Your images are automatically scanned with Trivy on every build. Check results in:
- GitHub Actions → Docker workflow → Trivy results
- Security tab → Code scanning alerts

### SBOM (Software Bill of Materials)

An SBOM is automatically generated for each release and attached to the workflow run.

## 🐛 Troubleshooting

### Build Fails with "permission denied"

Check that GitHub Actions has write permissions:
1. Go to repo Settings → Actions → General
2. Scroll to "Workflow permissions"
3. Select "Read and write permissions"
4. Click Save

### Image Push Fails

```bash
# Check workflow logs
gh run view --log-failed

# Common issues:
# - Repository visibility settings
# - Package doesn't exist yet (will be created on first push)
# - Token permissions (should auto-configure)
```

### Can't Pull Image (403 Forbidden)

Make the package public:
1. Go to https://github.com/alonalmog82?tab=packages
2. Click on `asyncefspurge`
3. Package settings → Change visibility → Public

### Authentication Required When Pulling

For private packages, authenticate:
```bash
# Create a Personal Access Token (PAT) with read:packages scope
# https://github.com/settings/tokens

# Login
echo $GITHUB_PAT | docker login ghcr.io -u alonalmog82 --password-stdin

# Now pull
docker pull ghcr.io/alonalmog82/asyncefspurge:latest
```

## 📊 View Published Packages

### Your Packages Page
https://github.com/alonalmog82?tab=packages

### Package Details
https://github.com/alonalmog82/AsyncEFSPurge/pkgs/container/asyncefspurge

### View All Versions
```bash
# Using GitHub CLI
gh api /users/alonalmog82/packages/container/asyncefspurge/versions | jq '.[].metadata.container.tags'
```

## ✅ Quick Start Checklist

- [ ] Repository created on GitHub
- [ ] Code pushed to main branch
- [ ] Tag created and pushed (v1.0.0)
- [ ] GitHub Actions workflow ran successfully
- [ ] Package visible at https://github.com/alonalmog82?tab=packages
- [ ] Package set to public
- [ ] Successfully pulled: `docker pull ghcr.io/alonalmog82/asyncefspurge:latest`
- [ ] Image runs correctly: `docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version`

## 🎉 Success!

Once you see your package at https://github.com/alonalmog82?tab=packages, you're done!

Others can now use your image:
```bash
docker pull ghcr.io/alonalmog82/asyncefspurge:latest
docker run --rm -v /mnt/efs:/data \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --max-age-days 30 --dry-run
```

## 📚 Additional Resources

- [GitHub Container Registry Docs](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Docker Build Push Action](https://github.com/marketplace/actions/build-and-push-docker-images)

