# 🚀 Quick Start: Publishing to GHCR

## One-Command Publishing (Automated)

```bash
./scripts/publish-first-release.sh
```

This script will:
- ✅ Create GitHub repository (if needed)
- ✅ Commit any changes
- ✅ Push to main branch
- ✅ Create v1.0.0 tag
- ✅ Trigger GitHub Actions to build and publish

---

## Manual Publishing (Step by Step)

### 1️⃣ Initialize Git and Push to GitHub

```bash
cd /Users/alonalmog/git/AsyncEFSPurge

# Initialize git (if not done)
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit: AsyncEFSPurge ready for publishing"

# Create GitHub repo (using GitHub CLI)
gh auth login
gh repo create AsyncEFSPurge --public --source=. --remote=origin --push

# Or manually add remote
git remote add origin https://github.com/alonalmog82/AsyncEFSPurge.git
git branch -M main
git push -u origin main
```

### 2️⃣ Create and Push Version Tag

```bash
# Create version tag
git tag -a v1.0.0 -m "Release v1.0.0 - Initial release"

# Push tag to trigger build
git push origin v1.0.0
```

### 3️⃣ Monitor Build

```bash
# Watch GitHub Actions
open https://github.com/alonalmog82/AsyncEFSPurge/actions

# Or using CLI
gh run watch
```

### 4️⃣ Make Package Public

After first build completes:

1. Go to: https://github.com/alonalmog82?tab=packages
2. Click on `asyncefspurge`
3. Click "Package settings"
4. Scroll to "Change visibility"
5. Select "Public"
6. Confirm

### 5️⃣ Test Your Published Image

```bash
# Pull image
docker pull ghcr.io/alonalmog82/asyncefspurge:latest

# Verify version
docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version

# Test with data
docker run --rm -v ~/test-data:/data \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --max-age-days 30 --dry-run
```

---

## 🎯 Your Published Images

After successful build, your images will be available at:

```
ghcr.io/alonalmog82/asyncefspurge:latest   # Always latest release
ghcr.io/alonalmog82/asyncefspurge:1.0.0    # Specific version
ghcr.io/alonalmog82/asyncefspurge:1.0      # Minor version
ghcr.io/alonalmog82/asyncefspurge:1        # Major version
ghcr.io/alonalmog82/asyncefspurge:main     # Latest from main branch
```

---

## 🔄 Future Releases

### For New Versions

```bash
# 1. Update version in code
echo '__version__ = "1.1.0"' > src/efspurge/__init__.py
sed -i '' 's/version = "1.0.0"/version = "1.1.0"/' pyproject.toml

# 2. Commit and tag
git add .
git commit -m "feat: new feature - bump to v1.1.0"
git tag -a v1.1.0 -m "Release v1.1.0"
git push origin main --tags

# Done! GitHub Actions will build and publish automatically
```

---

## 🐛 Troubleshooting

### Build fails with permissions error

1. Go to: https://github.com/alonalmog82/AsyncEFSPurge/settings/actions
2. Scroll to "Workflow permissions"
3. Select "Read and write permissions"
4. Save

### Can't pull image (403 error)

Make package public (see step 4 above)

### Want to check build logs

```bash
gh run list --workflow=Docker
gh run view <run-id> --log
```

---

## 📚 Full Documentation

- **PUBLISHING.md** - Complete publishing guide with troubleshooting
- **README.md** - User documentation and usage examples
- **CONTRIBUTING.md** - Development and contribution guide

---

## ✅ Success Checklist

- [ ] Repository created on GitHub
- [ ] Tag v1.0.0 pushed
- [ ] GitHub Actions workflow completed successfully
- [ ] Package made public
- [ ] Successfully pulled: `docker pull ghcr.io/alonalmog82/asyncefspurge:latest`
- [ ] Image runs: `docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version`

---

**🎉 That's it! Your container is now published to GHCR!**

View your packages: https://github.com/alonalmog82?tab=packages

