from __future__ import annotations

from typing import Protocol

from .models import NormalizedEmailMessage


class EmailIntakeProvider(Protocol):
    def list_messages(self) -> list[NormalizedEmailMessage]: ...


class FakeEmailIntakeProvider:
    def __init__(self, messages: list[NormalizedEmailMessage] | None = None):
        self._messages = list(messages or [])
        self.fetch_count = 0

    def add_message(self, message: NormalizedEmailMessage) -> None:
        self._messages.append(message)

    def list_messages(self) -> list[NormalizedEmailMessage]:
        self.fetch_count += 1
        return list(self._messages)
