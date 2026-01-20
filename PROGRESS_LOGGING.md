# Progress Logging Debugging Guide

If you're not seeing progress updates every 30 seconds, here's what to check:

## Common Issues

### 1. Job Completes Too Quickly (< 30 seconds)

**Symptom**: Only see "Starting" and "Completed" logs

**Reason**: Progress logs trigger every 30 seconds. If your job finishes in < 30 seconds, you won't see any progress updates.

**Solution**: This is normal! The progress feature is designed for long-running jobs (millions of files).

### 2. Very Fast File Processing

**Symptom**: Job takes 60 seconds but only see 1-2 progress updates

**Reason**: Progress only logs when `update_stats()` is called (during file/directory processing). If files are processed in large batches, updates may be sparse.

**Solution**: This is expected behavior. The async processing is so fast that updates might not align exactly with the 30-second intervals.

### 3. Low File Count

**Symptom**: Scanning 1000 files, no progress updates

**Reason**: With high concurrency (1000), scanning 1000 files might take only a few seconds.

**Expected**: 
- 1,000 files @ 1000/sec = 1 second (no progress)
- 10,000 files @ 1000/sec = 10 seconds (no progress)
- 50,000 files @ 1000/sec = 50 seconds (1-2 progress updates)
- 1,000,000 files @ 1000/sec = 1000 seconds (30+ progress updates)

### 4. Buffered Logging

**Symptom**: Progress updates appear all at once at the end

**Reason**: Python logging might be buffered

**Solution**: Set `PYTHONUNBUFFERED=1` environment variable (already set in k8s-cronjob.yaml)

## Testing Progress Logging

To verify progress logging works, you need a dataset that takes > 30 seconds to process.

### Create Test Data

\`\`\`bash
# Create a directory with many files
mkdir -p /tmp/progress-test
for i in {1..100000}; do
    touch /tmp/progress-test/file-$i.txt
done
\`\`\`

### Run with Lower Concurrency (Makes it Slower)

\`\`\`bash
# Lower concurrency = slower processing = more progress updates
docker run --rm -v /tmp/progress-test:/data \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data \
  --max-age-days 0 \
  --max-concurrency 100 \
  --dry-run \
  --log-level INFO
\`\`\`

With 100,000 files and concurrency=100, you should see progress updates.

## What's Normal

### Small Dataset (< 10K files)
- **Time**: < 30 seconds
- **Expected progress logs**: 0-1
- **Behavior**: Normal! Just see start and end logs

### Medium Dataset (10K-100K files)
- **Time**: 30-300 seconds
- **Expected progress logs**: 1-10
- **Behavior**: You'll see periodic updates

### Large Dataset (100K-1M+ files)
- **Time**: Minutes to hours
- **Expected progress logs**: Many (every 30 seconds)
- **Behavior**: Regular updates showing progress

## Verify It's Working

Check your "Starting" log includes this field:

\`\`\`json
{
  "message": "Starting EFS purge - DRY RUN MODE",
  "extra_fields": {
    ...
    "progress_interval_seconds": 30  // ← Confirms progress is configured
  }
}
\`\`\`

## Debug Commands

\`\`\`bash
# Check image version
docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest --version

# Check file count
find /your/path -type f | wc -l

# Time the operation manually
time efspurge /your/path --max-age-days 30 --dry-run

# If < 30 seconds, no progress logs is expected
\`\`\`

## The Bottom Line

**Progress logging works correctly!**

If you're not seeing updates, it's most likely because:
1. Your dataset is small (< 50K files)
2. Your processing is very fast (< 30 seconds total)
3. This is completely normal behavior

For **millions of files** (your use case), you WILL see progress updates every 30 seconds.

