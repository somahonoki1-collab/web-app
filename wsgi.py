"""Production WSGI entry point; never starts the development server."""
from runtime_config import configure_environment

configure_environment(production=True)

from app import app as application  # noqa: E402
