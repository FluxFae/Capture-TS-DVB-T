"""Single source of truth for the application version.

The OpenAPI schema announces it and the footer renders it, so it is defined
once here and imported everywhere else. Never repeat it as a literal.
"""

APP_VERSION = "0.4.0"
