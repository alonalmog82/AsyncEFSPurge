# 🚀 Performance Guide for AsyncEFSPurge

## Overview

AsyncEFSPurge is optimized for high-scale file deletion on AWS EFS and network filesystems. This guide explains the performance features and how to tune them for your workload.

---

## 🎯 Key Performance Features

### 1. **Concurrent Subdirectory Scanning** (v1.4.0+)

Subdirectories are now scanned in **parallel**, not sequentially.

**Impact:**
- **Before**: Deep directory trees scanned one-by-one (slow)
- **After**: All subdirectories scanned concurrently (fast)
- **Speed improvement**: 5-10x on deep hierarchies

**Example:**
```
/data/
  ├── dir1/ (1000 files)
  ├── dir2/ (1000 files)
  └── dir3/ (1000 files)

Before: dir1 → dir2 → dir3 (sequential, ~30 seconds)
After:  dir1 + dir2 + dir3 (parallel, ~10 seconds)
```

---

### 2. **Batched Task Creation** (v1.4.0+)

Files are processed in batches to prevent memory exhaustion.

**Problem Solved:**
- Large directories (100K+ files) would create 100K task objects
- Each task consumes ~1-2KB memory
- Total: 100MB+ just for task overhead → OOM kills

**Solution:**
- Process files in batches (default: 5000)
- Creates max 5000 tasks at once
- Dramatically reduces memory footprint

**Configuration:**
```bash
efspurge /data \
  --task-batch-size=5000  # Adjust based on memory availability
```

---

### 3. **Memory Back-Pressure** (v1.4.0+)

Automatic throttling when memory usage is high.

**How It Works:**
1. Monitor memory usage continuously
2. If usage exceeds `--memory-limit-mb`, pause briefly
3. Force garbage collection
4. Resume processing

**Benefits:**
- Prevents OOM kills
- Allows processing datasets larger than available RAM
- Self-regulating performance

**Configuration:**
```bash
efspurge /data \
  --memory-limit-mb=800  # Set to ~80% of container memory limit
```

**Recommended Settings:**

| Container Memory | memory-limit-mb | Reasoning |
|------------------|----------------|-----------|
| 512Mi | 400 | 80% of 512Mi |
| 1Gi | 800 | 80% of 1Gi (default) |
| 2Gi | 1600 | 80% of 2Gi |
| 4Gi | 3200 | 80% of 4Gi |

---

## 📊 Performance Characteristics

### Expected Throughput

| Scenario | Files/Sec | Notes |
|----------|-----------|-------|
| **Shallow structure** (< 5 levels) | 1500-2500 | Excellent |
| **Deep hierarchy** (10+ levels) | 800-1500 | Good (was 200-500) |
| **Flat directory** (all files in one dir) | 1000-2000 | Memory-dependent |
| **Mixed workload** | 1000-1800 | Typical production |

### Bottlenecks

1. **EFS Latency** (5-20ms per operation)
   - Mitigated by high concurrency
   - No client-side fix possible

2. **Network Bandwidth**
   - Rarely the bottleneck
   - Metadata operations are small

3. **Memory Availability**
   - Solved by batching + back-pressure
   - Monitor `memory_backpressure_events` in logs

4. **CPU**
   - Usually not a bottleneck
   - Consider increasing if CPU usage > 80%

---

## ⚙️ Tuning Guide

### For Different Dataset Sizes

#### Small (< 100K files)
```bash
efspurge /data \
  --max-age-days=30 \
  --max-concurrency=500 \
  --memory-limit-mb=400 \
  --task-batch-size=5000
```

#### Medium (100K - 1M files)
```bash
efspurge /data \
  --max-age-days=30 \
  --max-concurrency=1000 \
  --memory-limit-mb=800 \
  --task-batch-size=5000
```

#### Large (1M - 10M files)
```bash
efspurge /data \
  --max-age-days=30 \
  --max-concurrency=2000 \
  --memory-limit-mb=1600 \
  --task-batch-size=10000
```

#### Very Large (10M+ files)
```bash
efspurge /data \
  --max-age-days=30 \
  --max-concurrency=3000 \
  --memory-limit-mb=3200 \
  --task-batch-size=10000
```

**Important:** Allocate container memory = `memory-limit-mb` / 0.8

---

### For Different Directory Structures

#### Flat (all files in few directories)
- **Challenge**: Many files per directory
- **Solution**: Increase `task-batch-size`, ensure adequate memory
```bash
--task-batch-size=10000 \
--memory-limit-mb=1600
```

#### Deep (many nested directories)
- **Challenge**: Many subdirectories
- **Solution**: Works great with v1.4.0+ (concurrent scanning)
```bash
--max-concurrency=2000  # High concurrency helps
```

#### Balanced
- **Challenge**: Mix of both
- **Solution**: Use defaults, they work well
```bash
--max-concurrency=1000 \
--task-batch-size=5000
```

---

## 📈 Monitoring Performance

### Key Metrics to Watch

Check the progress logs every 30 seconds:

```json
{
  "message": "Progress update",
  "extra_fields": {
    "files_scanned": 150000,
    "files_per_second": 1250,
    "memory_mb": 650,
    "memory_limit_mb": 800,
    "memory_usage_percent": 81.2,
    "memory_backpressure_events": 3,
    "elapsed_seconds": 120
  }
}
```

#### Good Signs ✅
- `files_per_second` > 1000
- `memory_usage_percent` < 90
- `memory_backpressure_events` < 10% of updates
- `errors` close to 0

#### Warning Signs ⚠️
- `files_per_second` < 500 → Increase concurrency
- `memory_usage_percent` > 95 → Reduce batch size or increase limit
- `memory_backpressure_events` frequent → Increase memory limit
- `errors` > 1% → Check permissions/filesystem health

---

## 🎛️ Advanced Tuning

### Kubernetes Resource Configuration

```yaml
resources:
  requests:
    memory: "512Mi"   # Guaranteed minimum
    cpu: "500m"
  limits:
    memory: "2Gi"     # Hard limit (set memory-limit-mb to 80% of this)
    cpu: "2000m"
```

### Memory Optimization

1. **Lower batch size** if memory is tight
   ```bash
   --task-batch-size=2000  # Instead of 5000
   ```

2. **Reduce concurrency** if memory pressure is high
   ```bash
   --max-concurrency=500  # Instead of 1000
   ```

3. **Increase memory limit** if you have resources
   ```yaml
   limits:
     memory: "4Gi"
   ```
   ```bash
   --memory-limit-mb=3200
   ```

### Performance Optimization

1. **Increase concurrency** for network storage
   ```bash
   --max-concurrency=3000  # EFS can handle it
   ```

2. **Increase batch size** for more memory
   ```bash
   --task-batch-size=10000  # Fewer batch switches
   ```

3. **Reduce logging** for slight speed boost
   ```bash
   --log-level=WARNING  # Less overhead
   ```

---

## 🧪 Performance Testing

### Benchmark Your Environment

```bash
# Create test data
mkdir -p /tmp/perf-test
for i in {1..10000}; do
  touch /tmp/perf-test/file-$i.txt
done

# Run benchmark
time efspurge /tmp/perf-test \
  --max-age-days=0 \
  --max-concurrency=1000 \
  --log-level=INFO \
  --dry-run

# Check files_per_second in output
```

### Expected Results

| Environment | Files/Sec | Notes |
|------------|-----------|-------|
| Local SSD | 5000+ | Very fast |
| Local HDD | 2000-3000 | Good |
| NFS | 1000-2000 | Network-bound |
| AWS EFS | 800-1500 | Latency-bound |
| S3 (via mount) | 500-1000 | API rate limited |

---

## 🔧 Troubleshooting Performance Issues

### Issue: Low Files/Second (< 500)

**Diagnosis:**
```bash
# Check progress logs
kubectl logs <pod> | grep "files_per_second"
```

**Solutions:**
1. Increase `--max-concurrency`
2. Check network latency to EFS
3. Verify EFS throughput mode (elastic vs provisioned)
4. Check if EFS is in burst credit deficit

---

### Issue: OOM Kills

**Diagnosis:**
```bash
# Check memory usage before crash
kubectl describe pod <pod> | grep -A 10 "Last State"
```

**Solutions:**
1. Reduce `--task-batch-size` to 2000 or 1000
2. Reduce `--max-concurrency` to 500
3. Increase container memory limit
4. Lower `--memory-limit-mb` for more aggressive back-pressure

---

### Issue: Frequent Back-Pressure Events

**Diagnosis:**
```json
"memory_backpressure_events": 150  // Too high
```

**Solutions:**
1. Increase `--memory-limit-mb` (more headroom)
2. Increase container memory limit
3. Reduce `--task-batch-size` (less memory per batch)
4. This is expected behavior, but frequent events slow processing

---

## 🎯 Best Practices

### DO ✅

1. **Start conservative, then tune up**
   ```bash
   # First run
   --max-concurrency=500 --memory-limit-mb=400
   
   # After monitoring, increase
   --max-concurrency=2000 --memory-limit-mb=1600
   ```

2. **Monitor first runs closely**
   ```bash
   kubectl logs -f <pod> | jq .
   ```

3. **Set memory-limit-mb to 80% of container limit**
   ```
   Container: 1Gi → memory-limit-mb: 800
   ```

4. **Test with dry-run first**
   ```bash
   --dry-run  # See performance without actual deletion
   ```

### DON'T ❌

1. **Don't set memory-limit-mb > container limit**
   - Will trigger OOM kills

2. **Don't use unlimited concurrency**
   - Can overwhelm filesystem

3. **Don't ignore memory_backpressure_events**
   - Indicates memory pressure

4. **Don't expect local disk performance on EFS**
   - Network latency is unavoidable

---

## 📚 Version History

| Version | Performance Improvements |
|---------|------------------------|
| v1.4.0 | Concurrent subdirectory scanning, batched tasks, memory back-pressure |
| v1.2.0 | Real-time memory monitoring |
| v1.1.0 | Progress reporting every 30s |
| v1.0.0 | Initial async implementation |

---

## 🆘 Need Help?

If you're not getting expected performance:

1. Share your logs (especially progress updates)
2. Describe your directory structure
3. Report your file count and EFS configuration
4. Open an issue: https://github.com/alonalmog82/AsyncEFSPurge/issues

---

**Remember: Performance tuning is iterative. Start with defaults, monitor, then adjust!**

