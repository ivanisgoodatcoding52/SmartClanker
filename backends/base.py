"""
Backend interface for quiz bowl question sources.

To add a new backend:
  1. Create backends/yourname.py
  2. Subclass QuizBackend, set CATEGORIES / DIFFICULTY_TIERS, and implement
     fetch_random_tossup() and check_answer()
  3. Register it in backends/__init__.py's BACKENDS dict
  4. Run the bot with QUIZ_BACKEND=yourname

A tossup dict returned by fetch_random_tossup() must have at least:
  "question" - the tossup text. May contain HTML tags (bot.py strips them) and,
               if power is supported, the literal marker "(*)" placed right after
               the last word of the power clues (MODAQ/qbreader convention).
  "answer"   - the answerline. May contain HTML tags (bot.py strips them).
Optional keys "category" / "subcategory" are shown in the round header if present.

check_answer() must return a dict with a "directive" key: "accept", "reject", or
"prompt". For "prompt", also include a "directedPrompt" string with follow-up
text to show the player (used for answerlines that need more specificity).
If your source has no answer-checking endpoint, do a normalized string-contains
check locally and return "accept"/"reject" — see qbreader.py's fallback for
a minimal example.
"""

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
        """Return one tossup dict (see module docstring for the shape), or
        None if a tossup couldn't be fetched (network error, empty result,
        etc). Never raise — the bot expects failures to come back as None."""
        ...

    @abstractmethod
    async def check_answer(self, answerline: str, given_answer: str) -> dict:
        """Return {"directive": "accept" | "reject" | "prompt", ...}. Never
        raise — on failure, fall back to a local best-effort check."""
        ...
