# 🛡️ Production Safety Guide for AsyncEFSPurge

## ⚠️ Critical Warning

**This tool permanently deletes files with no recovery option.**

Before running in production, understand that:
- ❌ There is NO undo
- ❌ Deleted files are gone forever (unless you have backups)
- ❌ One wrong parameter can delete critical data
- ✅ You MUST test thoroughly before production use

---

## 🔐 Security & Safety Features

### Built-In Safety Features

1. **Dry-run mode is default** - Must explicitly disable
2. **Non-root container user** - Runs as UID 1000
3. **Symlink protection** - Won't follow symbolic links
4. **Error isolation** - Individual failures don't crash operation
5. **Comprehensive audit logging** - JSON logs of all operations
6. **Permission handling** - Graceful failure on access denied

### Docker Security

1. **Read-only root filesystem** - Container can't modify itself
2. **No privilege escalation** - Can't gain root access
3. **Minimal base image** - Python 3.11-slim with minimal attack surface
4. **Multi-stage build** - No build tools in final image
5. **Dropped capabilities** - Runs with minimal Linux capabilities

---

## ⚠️ Risk Assessment

### HIGH RISK Scenarios

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Wrong path specified | Data loss | Medium | Always test with dry-run first |
| Incorrect age threshold | Data loss | Medium | Start conservative (90+ days) |
| Timezone confusion | Premature deletion | Low | Test with known old files |
| Container gets write access to wrong mount | Catastrophic | Low | Use read-only mounts where possible |
| Bug in file age calculation | Data loss | Very Low | Thoroughly tested |

### MEDIUM RISK Scenarios

| Risk | Impact | Mitigation |
|------|--------|------------|
| Partial operation failure | Incomplete cleanup | Monitor logs, retry mechanism |
| High concurrency overwhelms filesystem | Performance degradation | Tune --max-concurrency |
| Permission errors | Failed deletions | Run with appropriate user/group |

---

## ✅ Production Safety Checklist

### Phase 1: Pre-Production Testing (REQUIRED)

- [ ] **Create test environment matching production**
  ```bash
  # Create test directory structure
  mkdir -p ~/test-efs/{keep,delete}
  
  # Add files to keep (new)
  touch ~/test-efs/keep/important-{1..10}.txt
  
  # Add files to delete (old)
  touch ~/test-efs/delete/old-{1..10}.txt
  touch -t 202301010000 ~/test-efs/delete/old-*.txt
  ```

- [ ] **Test dry-run mode thoroughly**
  ```bash
  docker run --rm -v ~/test-efs:/data \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data --max-age-days 90 --dry-run --log-level INFO
  
  # Verify output shows correct files
  # Verify no files were deleted
  ```

- [ ] **Test actual deletion on test data**
  ```bash
  docker run --rm -v ~/test-efs:/data \
    ghcr.io/alonalmog82/asyncefspurge:latest \
    /data --max-age-days 90 --log-level INFO
  
  # Verify only old files were deleted
  # Verify new files remain
  ```

- [ ] **Verify timezone handling**
  ```bash
  # Check container timezone
  docker run --rm ghcr.io/alonalmog82/asyncefspurge:latest \
    /bin/sh -c "date"
  
  # Use TZ environment variable if needed
  docker run --rm -e TZ=America/New_York ...
  ```

- [ ] **Test with various age thresholds**
  ```bash
  # Try different values
  --max-age-days 30   # 1 month
  --max-age-days 90   # 3 months (RECOMMENDED START)
  --max-age-days 180  # 6 months (SAFEST)
  ```

- [ ] **Review logs format and content**
  ```bash
  # Ensure logs are captured properly
  docker run ... 2>&1 | tee purge.log
  
  # Check log structure
  cat purge.log | jq '.'
  ```

### Phase 2: Limited Production Trial

- [ ] **Start with conservative settings**
  ```bash
  # First production run - VERY CONSERVATIVE
  --max-age-days 180  # Only delete files older than 6 months
  --dry-run          # Always start with dry-run
  ```

- [ ] **Run on smallest/least critical dataset first**
  ```bash
  # Pick a non-critical subdirectory
  /mnt/efs/temp-files
  /mnt/efs/logs-archive
  ```

- [ ] **Monitor first runs closely**
  - Watch logs in real-time
  - Verify correct files are targeted
  - Check disk space freed matches expectations

- [ ] **Gradual rollout**
  ```bash
  Week 1: --max-age-days 180 --dry-run (observe only)
  Week 2: --max-age-days 180 (actual deletion, conservative)
  Week 3: --max-age-days 90 (if comfortable)
  Week 4: --max-age-days 30 (full production)
  ```

### Phase 3: Production Deployment

- [ ] **Document your configuration**
  ```bash
  # Create a config file documenting decisions
  cat > efs-purge-config.txt <<EOF
  Path: /mnt/efs/data
  Age threshold: 30 days
  Reason: Data retention policy requires 30-day history
  Approved by: [Name]
  Date: [Date]
  Schedule: Daily at 2 AM UTC
  EOF
  ```

- [ ] **Set up monitoring and alerts**
  - Log retention for audit trail
  - Alert on high error counts
  - Alert on unexpected deletion volumes
  - Dashboard showing files purged over time

- [ ] **Implement safeguards**
  ```yaml
  # Use resource limits
  resources:
    limits:
      memory: "512Mi"
      cpu: "1000m"
  
  # Set job timeout
  activeDeadlineSeconds: 7200  # 2 hours max
  
  # Limit retries
  backoffLimit: 1  # Don't retry failed jobs automatically
  ```

- [ ] **Create runbook for incidents**
  - What to do if wrong files deleted
  - How to restore from backup
  - Who to contact
  - How to pause/disable the job

### Phase 4: Ongoing Operations

- [ ] **Regular log review**
  ```bash
  # Weekly review of deletion patterns
  kubectl logs -l app=efspurge --since=7d | jq .
  ```

- [ ] **Backup verification**
  ```bash
  # Ensure backups exist and are restorable
  # Test restoration periodically
  ```

- [ ] **Version pinning**
  ```yaml
  # Don't use :latest in production
  image: ghcr.io/alonalmog82/asyncefspurge:1.0.0  # Pin specific version
  ```

- [ ] **Change control process**
  - Document any configuration changes
  - Test changes in staging first
  - Require approval for age threshold changes

---

## 🚨 Emergency Procedures

### If Wrong Files Are Deleted

1. **Immediately stop the job**
   ```bash
   kubectl delete job efspurge-xxxxx
   kubectl delete cronjob efs-purge  # Prevent future runs
   ```

2. **Assess damage**
   ```bash
   # Review logs to see what was deleted
   kubectl logs job/efspurge-xxxxx | jq '.message'
   ```

3. **Restore from backup**
   ```bash
   # Use your backup solution (AWS Backup, snapshots, etc.)
   aws backup start-restore-job ...
   ```

4. **Root cause analysis**
   - What went wrong?
   - How to prevent in future?
   - Update this document

### Pause Operations

```bash
# Kubernetes: Delete CronJob
kubectl delete cronjob efs-purge

# Docker Compose: Stop service
docker-compose stop efspurge-cron

# ECS: Set desired count to 0
aws ecs update-service --desired-count 0 --service efs-purge
```

---

## 🔒 Additional Security Hardening

### Least Privilege Principle

```yaml
# Kubernetes: Use specific service account
serviceAccountName: efspurge-sa

# Grant only necessary permissions
# Don't give cluster-wide access
```

### Network Policies

```yaml
# Limit network access (if needed)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: efspurge-netpol
spec:
  podSelector:
    matchLabels:
      app: efspurge
  policyTypes:
  - Egress
  egress:
  - to:
    - podSelector: {}  # Only pod-to-pod traffic
```

### Read-Only Mounts (Testing)

```bash
# For testing, mount as read-only to ensure no changes
docker run --rm -v /mnt/efs:/data:ro \
  ghcr.io/alonalmog82/asyncefspurge:latest \
  /data --dry-run
```

### Audit Logging

```yaml
# Ensure all logs are captured
volumes:
- name: audit-logs
  emptyDir: {}

# Save logs to persistent storage
# Forward to SIEM/log aggregation
```

---

## 📊 Recommended Production Configuration

### Conservative (Safest)

```bash
# Start here
docker run --rm -v /mnt/efs:/data \
  ghcr.io/alonalmog82/asyncefspurge:1.0.0 \
  /data \
  --max-age-days 180 \
  --max-concurrency 500 \
  --log-level INFO
```

### Moderate (After Testing)

```bash
docker run --rm -v /mnt/efs:/data \
  ghcr.io/alonalmog82/asyncefspurge:1.0.0 \
  /data \
  --max-age-days 90 \
  --max-concurrency 1000 \
  --log-level INFO
```

### Aggressive (Well-Tested Environments Only)

```bash
docker run --rm -v /mnt/efs:/data \
  ghcr.io/alonalmog82/asyncefspurge:1.0.0 \
  /data \
  --max-age-days 30 \
  --max-concurrency 2000 \
  --log-level INFO
```

---

## 🎯 Best Practices Summary

### DO ✅

- Always test with `--dry-run` first
- Start with conservative age thresholds (180+ days)
- Pin to specific image versions in production
- Monitor logs and set up alerts
- Have verified backups
- Test restoration procedures
- Document all configuration decisions
- Use gradual rollout approach
- Review logs regularly

### DON'T ❌

- Don't use in production without thorough testing
- Don't use `:latest` tag in production
- Don't skip dry-run testing
- Don't set aggressive age thresholds without testing
- Don't run without backup strategy
- Don't ignore permission errors in logs
- Don't run with unlimited concurrency on first try
- Don't deploy during business hours (first time)

---

## 📈 Success Metrics

Track these to ensure safe operations:

- **Files scanned per run** - Should be consistent
- **Files purged per run** - Should match expectations
- **Error rate** - Should be near zero
- **Execution time** - Should be predictable
- **Disk space freed** - Should match file count × avg size

---

## 🆘 Support

If you encounter issues:

1. **Check logs first**
   ```bash
   kubectl logs -l app=efspurge --tail=100
   ```

2. **GitHub Issues**
   https://github.com/alonalmog82/AsyncEFSPurge/issues

3. **Review documentation**
   - README.md
   - CONTRIBUTING.md
   - This file

---

## ⚖️ Risk vs. Benefit

**Benefits:**
- Automated file cleanup
- Reclaim disk space
- Compliance with data retention policies
- Reduced storage costs

**Risks:**
- Accidental data loss
- Service disruption if misconfigured
- Performance impact on filesystem

**Verdict:** Safe for production IF you follow this guide and test thoroughly.

---

**Remember: This tool is as safe as you make it. Test, test, test!**

