"""Single source of truth for the package version.

Read statically by the build backend (see [tool.setuptools.dynamic] in
pyproject.toml) and re-exported from the package __init__, so the version is
defined in exactly one place. Keep this a plain string literal so the build
backend can parse it without importing the package.
"""

__version__ = "1.6.1"
