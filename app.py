"""Vercel Python entrypoint.

Tests import convoplex.api.create_app and must not build the 500-customer
database as a side effect of importing the package. This module is the one
Vercel loads.
"""

from convoplex.api import create_app

app = create_app()
