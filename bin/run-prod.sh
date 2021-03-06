#!/bin/bash -ex

echo "$GIT_SHA" > static/revision.txt
exec gunicorn wsgi.app --bind "0.0.0.0:${PORT:-8000}" --error-logfile - --access-logfile - \
                       --workers "${WSGI_NUM_WORKERS:-2}" \
                       --worker-class "${WSGI_WORKER_CLASS:-sync}" \
                       --log-level "${WSGI_LOG_LEVEL:-warning}"
