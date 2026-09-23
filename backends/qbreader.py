"""qbreader.org backend. Default question source."""

import asyncio
import re
from typing import Optional

import aiohttp

from .base import QuizBackend

_TAG_RE = re.compile(r"<[^>]+>")


class QBReaderBackend(QuizBackend):
    name = "qbreader"

    BASE_URL = "https://www.qbreader.org/api"

    CATEGORIES = [
        "Literature", "History", "Science", "Fine Arts", "Religion", "Mythology",
        "Philosophy", "Social Science", "Current Events", "Geography",
        "Other Academic", "Pop Culture",
    ]

    DIFFICULTY_TIERS = {
        "Easy (MS/HS-Easy)": [1, 2],
        "Regular (HS-Regs)": [3],
        "Hard (HS-Hard/Nats)": [4, 5],
        "Collegiate+": [6, 7, 8, 9, 10],
    }

    async def fetch_random_tossup(self, categories: set, difficulties: set) -> Optional[dict]:
        params = {"number": 1}
        if categories:
            params["categories"] = ",".join(categories)
        if difficulties:
            params["difficulties"] = ",".join(str(d) for d in difficulties)

        try:
            async with self.http.get(f"{self.BASE_URL}/random-tossup", params=params) as resp:
                if resp.status != 200:
                    return None
                try:
                    data = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    # e.g. a Cloudflare challenge/HTML page instead of JSON
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        tossups = data.get("tossups") if isinstance(data, dict) else data
        if not tossups:
            return None
        return tossups[0]

    async def check_answer(self, answerline: str, given_answer: str) -> dict:
        params = {"answerline": answerline, "givenAnswer": given_answer}
        try:
            async with self.http.get(f"{self.BASE_URL}/check-answer", params=params) as resp:
                if resp.status != 200:
                    return self._local_fallback_check(answerline, given_answer)
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    return self._local_fallback_check(answerline, given_answer)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return self._local_fallback_check(answerline, given_answer)

    @staticmethod
    def _local_fallback_check(answerline: str, given_answer: str) -> dict:
        """Rough local check used only if qbreader's API is unreachable or
        misbehaving (e.g. a Cloudflare challenge page)."""
        plain = _TAG_RE.sub("", answerline).lower()
        directive = "accept" if given_answer.strip().lower() in plain else "reject"
        return {"directive": directive}
