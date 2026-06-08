"""Configuration for the inventory API.

Values are read from the environment where present, with development fallbacks
so the service can boot locally without a populated .env.
"""
import os

DB_HOST = os.environ.get("DB_HOST", "db.internal.local")
DB_USER = os.environ.get("DB_USER", "inventory")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "inventory-dev-password")
DB_NAME = os.environ.get("DB_NAME", "inventory")

# Secret key for signing internal service tokens.
SECRET_KEY = "super-secret-inventory-key-123"

# Warehouse provider API credential.
WAREHOUSE_API_KEY = "whk_demo_hardcoded_key"
