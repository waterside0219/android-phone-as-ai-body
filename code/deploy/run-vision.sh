#!/bin/sh
# VPS only. The cron entry may call this every minute; flock keeps one daemon.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
umask 077
mkdir -p .private/vision
exec flock -n .private/vision/runner.lock /usr/bin/python3 -B server/body_vision.py --config deploy/vision.json run
