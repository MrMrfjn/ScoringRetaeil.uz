"""
WSGI entry point для Gunicorn / uWSGI и других серверов.
Использование: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:application
"""
import os
os.environ.setdefault("FLASK_ENV", "production")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from main import app

application = app
