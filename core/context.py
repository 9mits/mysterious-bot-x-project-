"""Module-level proxy singletons for bot, tree, and abuse_system."""
from __future__ import annotations

from typing import Optional

from discord import app_commands
from discord.ext import commands


_active_bot: Optional[commands.Bot] = None


def set_bot(bot: commands.Bot) -> None:
    global _active_bot
    _active_bot = bot


def get_bot() -> commands.Bot:
    if _active_bot is None:
        raise RuntimeError("MBX bot runtime has not been initialized yet.")
    return _active_bot


class BotProxy:
    def __getattr__(self, name: str):
        return getattr(get_bot(), name)

    def event(self, coro):
        return coro

    def command(self, *args, **kwargs):
        return commands.command(*args, **kwargs)


class AbuseSystemProxy:
    def __getattr__(self, name: str):
        return getattr(get_bot().abuse_system, name)


class TreeProxy:
    def __init__(self) -> None:
        self.added_commands = []
        self.error_handler = None

    def command(self, *args, **kwargs):
        return app_commands.command(*args, **kwargs)

    def context_menu(self, *args, **kwargs):
        return app_commands.context_menu(*args, **kwargs)

    def add_command(self, command, *args, **kwargs):
        self.added_commands.append((command, args, kwargs))
        return command

    def remove_command(self, *args, **kwargs):
        return None

    def error(self, coro):
        self.error_handler = coro
        return coro


bot = BotProxy()
abuse_system = AbuseSystemProxy()
tree = TreeProxy()
