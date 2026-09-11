from .models import (
    EmailAttachment,
    EmailAttachmentResult,
    EmailIntakeResult,
    EmailMessageMetadata,
    NormalizedEmailMessage,
)
from .providers import EmailIntakeProvider, FakeEmailIntakeProvider
from .gmail_provider import (
    GmailAdapterError,
    GmailAttachmentFetchError,
    GmailClient,
    GmailEmailIntakeProvider,
    GmailMalformedPayloadError,
    GmailMessageNotFoundError,
)
from .service import EmailIntakeService
from .gmail_sync import (
    GmailProcessedLabelAdapter,
    GmailProcessedLabelError,
    GmailProcessedLabelResult,
    GmailProcessedLabelSynchronizer,
    PROCESSED_LABEL_NAME,
)

__all__ = [
    "EmailAttachment",
    "EmailAttachmentResult",
    "GmailAdapterError",
    "GmailAttachmentFetchError",
    "GmailClient",
    "GmailEmailIntakeProvider",
    "GmailMalformedPayloadError",
    "GmailMessageNotFoundError",
    "EmailIntakeProvider",
    "EmailIntakeResult",
    "EmailIntakeService",
    "EmailMessageMetadata",
    "FakeEmailIntakeProvider",
    "GmailProcessedLabelAdapter",
    "GmailProcessedLabelError",
    "GmailProcessedLabelResult",
    "GmailProcessedLabelSynchronizer",
    "NormalizedEmailMessage",
    "PROCESSED_LABEL_NAME",
]
