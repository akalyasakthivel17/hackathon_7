"""
MongoDB connection helper.
Provides a singleton client and helper functions to access the database.
"""
import pymongo
from django.conf import settings


_client = None
_db = None


def get_client():
    """Get or create the MongoDB client (singleton)."""
    global _client
    if _client is None:
        _client = pymongo.MongoClient(settings.MONGODB_URI)
    return _client


def get_db():
    """Get the default database handle."""
    global _db
    if _db is None:
        _db = get_client()[settings.MONGODB_DB_NAME]
    return _db


def get_collection(name):
    """Get a collection by name."""
    return get_db()[name]
