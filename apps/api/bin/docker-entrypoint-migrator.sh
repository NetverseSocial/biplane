#!/bin/bash
set -e

python manage.py wait_for_db $1

python manage.py migrate $1
# Database-scoped system checks after migrate (bridge.E003 — RC 3484).
python manage.py check --database default