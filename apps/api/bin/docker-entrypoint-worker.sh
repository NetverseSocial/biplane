#!/bin/bash
set -e

python manage.py wait_for_db
# Wait for migrations
python manage.py wait_for_migrations
# Database-scoped system checks (bridge.E003 rename stability runs ONLY with
# an explicit --database; BaseCommand self-checks never pass it — RC 3484).
python manage.py check --database default
# Run the processes
celery -A plane worker -l info