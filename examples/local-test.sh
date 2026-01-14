# Example: Create sample test files
mkdir -p ./test-data/{old,new}
touch ./test-data/new/file{1..10}.txt

# Create old files (2 months ago)
touch -t $(date -d '60 days ago' +%Y%m%d%H%M) ./test-data/old/oldfile1.txt 2>/dev/null || \
touch -t $(date -v-60d +%Y%m%d%H%M) ./test-data/old/oldfile1.txt 2>/dev/null || \
echo "Manual timestamp update needed for MacOS users without gdate"

# Test dry run
efspurge ./test-data --max-age-days 30 --dry-run --log-level INFO

# Test actual purge (be careful!)
# efspurge ./test-data --max-age-days 30 --log-level INFO

# Clean up
rm -rf ./test-data

