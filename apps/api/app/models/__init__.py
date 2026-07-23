"""SQLAlchemy ORM models for the core schema (see docs/ARCHITECTURE.md).

Every model must be imported here so that ``Base.metadata`` (used by both
the app and Alembic's autogenerate) is fully populated as soon as this
package is imported.
"""

from app.core.db import Base
from app.models.api_key import ApiKey
from app.models.chunk import Chunk
from app.models.citation import Citation
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.message import Message
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "Base",
    "Organization",
    "User",
    "ApiKey",
    "Document",
    "Chunk",
    "Conversation",
    "Message",
    "Citation",
    "RefreshToken",
]
