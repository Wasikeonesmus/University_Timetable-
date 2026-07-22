import os
import sys
import subprocess
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SchedulerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'scheduler'

    def ready(self):
        # Register signals
        import scheduler.signals  # noqa: F401

        # Configure SQLite WAL mode and busy timeout for concurrent access
        from django.db.backends.signals import connection_created
        def _configure_sqlite(sender, connection, **kwargs):
            if connection.vendor == 'sqlite':
                cursor = connection.cursor()
                cursor.execute('PRAGMA journal_mode=WAL;')
                cursor.execute('PRAGMA synchronous=NORMAL;')
                cursor.execute('PRAGMA busy_timeout=60000;')
        connection_created.connect(_configure_sqlite)

        # Auto-start the django-q cluster worker as a subprocess so that
        # `python manage.py runserver` is the only command needed.
        self._maybe_start_qcluster()

    def _maybe_start_qcluster(self):
        # Only start when running the web server (not migrate, test, shell, etc.)
        running_server = (
            'runserver' in sys.argv
            or 'gunicorn' in sys.modules
            or 'uvicorn' in sys.modules
        )
        if not running_server:
            return

        # Django's autoreloader spawns the app twice:
        #   - Parent process: watches files, RUN_MAIN not set
        #   - Child process:  actually serves requests, RUN_MAIN='true'
        # We only launch qcluster from the child (or when --noreload is used).
        run_main = os.environ.get('RUN_MAIN')
        reloader_disabled = '--noreload' in sys.argv

        if not (run_main == 'true' or reloader_disabled):
            return

        # Prevent spawning a second cluster on hot-reloads
        if os.environ.get('QCLUSTER_STARTED') == '1':
            return
        os.environ['QCLUSTER_STARTED'] = '1'

        try:
            # Spawn qcluster as a separate OS process (same Python / manage.py).
            # Using subprocess.Popen (non-blocking) so the server continues to start.
            manage_py = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'manage.py',
            )
            proc = subprocess.Popen(
                [sys.executable, manage_py, 'qcluster'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                f"[QCluster] Worker subprocess started automatically (PID {proc.pid}). "
                "No need to run qcluster separately."
            )
        except Exception as exc:
            logger.error(f"[QCluster] Failed to auto-start worker subprocess: {exc}")
