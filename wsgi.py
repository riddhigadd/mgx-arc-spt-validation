"""WSGI entry point for production (gunicorn / systemd on DGX Spark)."""
from app import app as application

# gunicorn expects `application` or `app`
app = application
