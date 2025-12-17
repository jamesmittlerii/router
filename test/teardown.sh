#!/bin/bash

echo "--- Tearing down LV2 Chain ---"

# 1. Kill the processes
# We use pkill -f to find the command names regardless of arguments
# We hit dtach first, then jalv to be sure.
if pkill -f "jalv"; then
    echo "✔ Killed jalv processes"
else
    echo "  (No jalv processes found)"
fi

# 2. Kill any lingering dtach wrappers
# Sometimes dtach stays alive if jalv hangs
if pkill -f "dtach"; then
    echo "✔ Killed dtach sessions"
else
    echo "  (No dtach sessions found)"
fi

# 3. Clean up the file system
# Remove the sockets we created
rm -f /tmp/*.sock
echo "✔ Removed sockets (/tmp/*.sock)"

# Optional: Remove logs (Uncomment the next line if you want logs wiped too)
# rm -f /tmp/jalv-*.log && echo "✔ Removed log files"

# 4. Remove those temp directories jalv creates (Safe to do now that processes are dead)
find /tmp -maxdepth 1 -type d -name "jalv??????" -exec rm -rf {} +
echo "✔ Cleaned up jalv temp folders"

echo "--- Done. System is clean. ---"
