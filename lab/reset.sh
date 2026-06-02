#!/usr/bin/env bash
# Reset the lab world to a clean state between exercises (or between students
# on a shared box). Re-seeds the DB/tickets/secrets and clears the outbox.
set -euo pipefail
cd "$(dirname "$0")"
python setup_data.py
echo "Lab reset complete. outbox.log cleared."
