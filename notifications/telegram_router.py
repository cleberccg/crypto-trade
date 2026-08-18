from __future__ import annotations

from notifications.telegram_commands import TelegramCommands, CommandResponse


class TelegramRouter:
    def __init__(self, commands: TelegramCommands) -> None:
        self._commands = commands

    def dispatch_command(self, text: str, chat_id: str) -> CommandResponse:
        return self._commands.handle(command=text, chat_id=chat_id)
