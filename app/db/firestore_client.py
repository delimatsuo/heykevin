"""Firestore client singleton."""

from google.cloud import firestore

from app.config import settings

_client = None


def get_firestore_client() -> firestore.Client:
    """Get or create the Firestore client singleton."""
    global _client
    if _client is None:
        project = settings.firestore_project_id.strip() or None
        _client = firestore.Client(project=project)
    return _client
