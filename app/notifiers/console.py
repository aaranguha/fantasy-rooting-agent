"""Console notifier - always available, sends nothing anywhere."""

from __future__ import annotations

import sys

from .base import Notifier


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, *, stream=None, **kw) -> None:
        super().__init__(**kw)
        self.stream = stream or sys.stdout
        self.sent: list[tuple[str, str]] = []

    def _send(self, title: str, body: str) -> str:
        self.sent.append((title, body))
        print("\n" + "=" * 60, file=self.stream)
        print(title, file=self.stream)
        print("=" * 60, file=self.stream)
        print(body, file=self.stream)
        print("=" * 60 + "\n", file=self.stream)
        self.stream.flush()
        return "printed to console"
