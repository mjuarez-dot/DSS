#!/bin/bash

if [ -z "${POSTGRES_USER}" ]; then
    base_postgres_image_default_user='postgres'
    export POSTGRES_USER="${base_postgres_image_default_user}"
fi
export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

postgres_ready() {
python << END
import sys

import psycopg2

try:
    psycopg2.connect(
        dbname="${POSTGRES_DB}",
        user="${POSTGRES_USER}",
        password="${POSTGRES_PASSWORD}",
        host="${POSTGRES_HOST}",
        port="${POSTGRES_PORT}",
    )
except Exception as e:
    print (e)
except psycopg2.OperationalError:
    print("${POSTGRES_DB}")
    print("${POSTGRES_USER}")
    print("${POSTGRES_PASSWORD}")
    print("${POSTGRES_HOST}")
    print("${POSTGRES_PORT}")
    print("not ready")
    sys.exit(-1)
sys.exit(0)
print("ready")

END
}
until postgres_ready; do
  >&2 echo 'Waiting for PostgreSQL to become available...'
  sleep 1
done
>&2 echo 'PostgreSQL is available'

# Processing new migrations ...
if [ "${RUN_MAKEMIGRATIONS:-false}" = "true" ]; then
  python /web/manage.py makemigrations
fi

# Applying new migrations ...
python /web/manage.py migrate

# Applying collectstatic ...
echo "collectstatic"
python /web/manage.py collectstatic --noinput
# ls /static/assets/images -la
echo "preparing gunicorn configuration..." 
chmod u+x /web/docker/gunicorn_run

#sleep 5m
echo "running gunicorn..."
/web/docker/gunicorn_run
