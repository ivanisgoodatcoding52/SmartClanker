from abc import ABC, abstractmethod
from typing import Optional

import aiohttp


class QuizBackend(ABC):
    #: Shown in bot messages/logs (e.g. "Couldn't get a tossup from the X backend").
    name: str = "base"

    #: Category names offered in /qb categories and the panel's select menu.
    CATEGORIES: list = []

    #: Difficulty tier label -> whatever value(s) fetch_random_tossup() expects
    #: for that tier. Leave as {} if the backend has no difficulty concept.
    DIFFICULTY_TIERS: dict = {}

    def __init__(self, http_session: aiohttp.ClientSession):
        # Shared aiohttp session (created once in bot.py's on_ready), reused
        # across requests so backends don't each open their own connection pool.
        self.http = http_session

    @abstractmethod
    async def fetch_random_tossup(self, categories: set, difficulties: set) -> Optional[dict]:
        ...

    @abstractmethod
    async def check_answer(self, answerline: str, given_answer: str) -> dict:
        ...
