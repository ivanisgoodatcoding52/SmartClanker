# Quiz Bowl Discord Bot

A Discord bot that runs live quiz bowl tossups: reads the question out word by
word, lets players buzz in with a button, and scores power/regular tossups
MODAQ-style. Question source is pluggable — ships with a qbreader.org backend,
but you can point it at any source by writing a small adapter class.

## Features

- `/qb panel` — interactive panel with category/difficulty/speed selects and Start/Stop
- `/qb start [categories]` — starts a tossup immediately
- `/qb speed <preset>` — Slow / Normal / Fast / Very Fast reading speed
- `/qb stop` — stops the round running in the current channel
- `/qb categories` — lists valid categories for the active backend
- Buzz button freezes the reveal, gives the buzzer a timed window to answer,
  and resumes for everyone else on a miss
- Power scoring (15 pts before the power mark, 10 after) when the source
  provides one

## Setup

```bash
git clone <your-repo-url>
cd quizbowl-bot
pip install -r requirements.txt
cp .env.example .env
# edit .env: set DISCORD_BOT_TOKEN, optionally QUIZ_BACKEND
python bot.py
```

Your bot needs the **Message Content Intent** enabled in the
[Discord Developer Portal](https://discord.com/developers/applications)
(Bot page, under Privileged Gateway Intents) — it's required to read
players' typed answers.

## Adding a new backend

Question sources live under `backends/`. To add one:

1. Create `backends/yourname.py`, subclass `QuizBackend` (see
   `backends/base.py` for the full interface and `backends/qbreader.py`
   for a working reference implementation).
2. Set `CATEGORIES` and `DIFFICULTY_TIERS` for your source's taxonomy.
3. Implement `fetch_random_tossup()` and `check_answer()`.
4. Register the class in `backends/__init__.py`'s `BACKENDS` dict.
5. Run with `QUIZ_BACKEND=yourname`.

A tossup dict must include `question` and `answer` (HTML tags are stripped
automatically). If your source marks the power mark differently than the
literal `(*)` convention, translate it to `(*)` before returning.

## Project layout

```
bot.py              Discord logic: commands, buzzing, reveal timing, UI
backends/
  base.py            The interface every backend implements
  qbreader.py         Default backend (qbreader.org)
  __init__.py          Registry — add new backends here
requirements.txt
.env.example
```

## License

MIT — see [LICENSE](LICENSE).
