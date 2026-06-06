import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETKEY = os.environ.get("SETTINGS_SECRET_KEY", "dev-secret-key")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "")

if os.environ.get("POSTGRES_DB"):
    DATABASES_LOCAL = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB"),
            "USER": os.environ.get("POSTGRES_USER"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD"),
            "HOST": os.environ.get("POSTGRES_HOST", "db_dssh"),
            "PORT": int(os.environ.get("POSTGRES_PORT", 5432)),
        }
    }
else:
    DATABASES_LOCAL = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.path.join(BASE_DIR, "db.sqlite3"),
        }
    }

# import dj_database_url
# from decouple import config

# SECRETKEY =config('SETTINGS_SECRET_KEY'),
# DATABASES_LOCAL = {
#     'default': dj_database_url.config(
#         default=config('DATABASE_URL')
#     )
# }
