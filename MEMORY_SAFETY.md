# 🛡️ Memory Safety & Monitoring Guide

## Overview

AsyncEFSPurge now includes real-time memory monitoring to help you safely process millions of files without running out of memory.

## 📊 Memory Tracking Features

### What's Tracked

Every 30 seconds, the progress logs now include:

```json
{
  "message": "Progress update",
  "extra_fields": {
    "files_scanned": 150000,
    "memory_mb": 245.3,
    "memory_mb_per_1k_files": 1.64,
    "files_per_second": 507.8,
    ...
  }
}
```

- **`memory_mb`**: Current memory usage in megabytes
- **`memory_mb_per_1k_files`**: Memory per 1000 files (helps predict total usage)

### Minimal Overhead

- Memory check takes **~1-2ms** every 30 seconds
- **Negligible impact** on performance (<0.01%)
- Uses `psutil` library (fallback to `resource` module if unavailable)

---

## 🔒 Safety Recommendations

### Memory Limits by Dataset Size

| Files | Concurrency | Est. Memory | Recommended Limit |
|-------|-------------|-------------|-------------------|
| 100K | 500 | ~100 MB | 256 Mi |
| 1M | 1000 | ~500 MB | 1 Gi |
| 10M | 1000 | ~1-2 GB | 2 Gi |
| 100M+ | 500-1000 | ~5-10 GB | 8-16 Gi |

### Formula

**Approximate memory per file**: 0.5-1 MB per 1000 files with concurrency=1000

**Total estimate**: `(files / 1000) * 1 MB * (concurrency / 1000)`

---

## ⚙️ Kubernetes Configuration

### Conservative (Safe for Testing)

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "500m"
  limits:
    memory: "1Gi"  # Pod killed if exceeded
    cpu: "2000m"

args:
  - --max-concurrency=500  # Lower concurrency = less memory
```

### Aggressive (Large Datasets)

```yaml
resources:
  requests:
    memory: "2Gi"
    cpu: "1000m"
  limits:
    memory: "4Gi"  # More headroom
    cpu: "4000m"

args:
  - --max-concurrency=1000  # Higher concurrency = faster but more memory
```

### Production Best Practice

```yaml
resources:
  requests:
    memory: "1Gi"    # Guaranteed minimum
    cpu: "1000m"
  limits:
    memory: "2Gi"    # 2x requests for safety margin
    cpu: "2000m"

args:
  - --max-concurrency=1000
  - --max-age-days=30
  - --log-level=INFO  # Monitor memory in logs
```

---

## 📈 Monitoring Strategy

### Step 1: Dry-Run Test

```bash
# Start with dry-run to estimate memory usage
kubectl create job test-memory --from=cronjob/efs-purge

# Watch logs for memory usage
kubectl logs -f job/test-memory | grep -E "(Progress update|memory_mb)"
```

### Step 2: Analyze Memory Pattern

Look for:
- **Peak memory_mb**: Maximum memory used
- **memory_mb_per_1k_files**: Memory efficiency metric
- **Growth pattern**: Linear growth = good, exponential = problem

Example output:
```json
{"message": "Progress update", "extra_fields": {"files_scanned": 50000, "memory_mb": 125.5, "memory_mb_per_1k_files": 2.51}}
{"message": "Progress update", "extra_fields": {"files_scanned": 100000, "memory_mb": 248.2, "memory_mb_per_1k_files": 2.48}}
{"message": "Progress update", "extra_fields": {"files_scanned": 150000, "memory_mb": 371.5, "memory_mb_per_1k_files": 2.48}}
```

✅ **Good**: `memory_mb_per_1k_files` stays constant (~2.48)  
❌ **Bad**: `memory_mb_per_1k_files` keeps increasing

### Step 3: Calculate Total Needed

```
Total files: 10,000,000
memory_mb_per_1k_files: 2.5 (from dry-run)

Estimated memory: (10,000,000 / 1000) * 2.5 = 25,000 MB = ~25 GB
Safety margin (2x): 50 GB
```

### Step 4: Adjust Configuration

If memory is too high:

1. **Reduce concurrency**:
   ```yaml
   --max-concurrency=500  # Half the concurrency = half the memory
   ```

2. **Increase memory limit**:
   ```yaml
   limits:
     memory: "4Gi"  # Increase to match needs
   ```

3. **Process in batches**:
   ```bash
   # Run on subdirectories instead of root
   /data/2024-01  # Process by month
   /data/2024-02
   ```

---

## 🚨 Warning Signs

### Memory Growing Too Fast

```json
{"files_scanned": 10000, "memory_mb": 50, "memory_mb_per_1k_files": 5.0}
{"files_scanned": 20000, "memory_mb": 150, "memory_mb_per_1k_files": 7.5}  ⚠️ Growing!
{"files_scanned": 30000, "memory_mb": 300, "memory_mb_per_1k_files": 10.0} 🚨 Problem!
```

**Action**: Stop the job and reduce `--max-concurrency`

### Pod Being OOMKilled

```bash
kubectl describe pod efs-purge-xxx
# Look for: "Reason: OOMKilled"
```

**Action**:
1. Increase memory limit
2. OR reduce concurrency
3. OR process smaller subdirectories

---

## 🎯 Optimal Settings

### For AWS EFS (High Latency, Network Storage)

```yaml
# Optimize for network latency, not memory
args:
  - --max-concurrency=2000  # High concurrency hides latency
  
resources:
  limits:
    memory: "4Gi"  # Need more memory for high concurrency
```

### For Local/Fast Storage

```yaml
# Lower concurrency since disk is fast
args:
  - --max-concurrency=200  # Lower = less memory

resources:
  limits:
    memory: "512Mi"  # Less memory needed
```

---

## 💡 Tuning Tips

### If Memory Usage is Low (<50% of limit)

✅ You can:
- Increase `--max-concurrency` for faster processing
- Decrease memory limit to save resources

### If Memory Usage is High (>80% of limit)

⚠️ You should:
- Decrease `--max-concurrency` for safety
- Increase memory limit
- Monitor for OOMKills

### If Processing is Slow

Check the `files_per_second` metric:
- **<100 files/sec**: Increase concurrency
- **>1000 files/sec**: You're already optimal
- **Memory growing**: Reduce concurrency

---

## 📋 Pre-Production Checklist

Before running on millions of files:

- [ ] Run dry-run test on representative dataset
- [ ] Monitor memory usage for at least 5 minutes
- [ ] Calculate `memory_mb_per_1k_files` metric
- [ ] Estimate total memory needed
- [ ] Set memory limit to 2x estimated usage
- [ ] Set up alerting on memory usage >80%
- [ ] Have rollback plan (stop job command ready)
- [ ] Test with actual deletion on small subset first

---

## 🆘 Emergency: Stop High Memory Job

```bash
# Stop the job immediately
kubectl delete job efs-purge-xxxx

# Or delete the entire cronjob
kubectl delete cronjob efs-purge

# Check if pod was OOMKilled
kubectl describe pod efs-purge-xxx | grep -A 5 "Last State"
```

---

## 📚 Additional Resources

- **PRODUCTION_SAFETY.md**: Complete production safety checklist
- **README.md**: Usage examples and deployment guides
- **GitHub Issues**: Report memory issues or ask questions

---

**Remember**: It's always better to start conservative and scale up than to start aggressive and crash! 🛡️

