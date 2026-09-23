import asyncio
import html
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from dotenv import load_dotenv

from backends import get_backend_class

load_dotenv()  

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

QUIZ_BACKEND = os.environ.get("QUIZ_BACKEND", "qbreader")

API_TIMEOUT_SECONDS = 10.0

DEFAULT_WORD_DELAY = 0.30   # seconds between each word being revealed, default speed
ANSWER_WINDOW = 7.0         # seconds a buzzer gets to type their answer
ANSWER_GRACE_PERIOD = 6.0   # seconds to wait for a buzz after the full question is read

TARGET_EDIT_INTERVAL = 0.6  # seconds; well clear of Discord's per-channel edit bucket

CLAUSE_END_CHARS = (",", ".", ";", ":", "!", "?")

SPEED_PRESETS = {
    "Slow": 0.55,
    "Normal": 0.30,
    "Fast": 0.18,
    "Very Fast": 0.10,
}


import re  # noqa: E402
TAG_RE = re.compile(r"<[^>]+>")

POWER_MARK = "(*)"

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True

bot = commands.Bot(command_prefix="!qb-unused-", intents=intents)


@dataclass
class ChannelSettings:
    categories: set = field(default_factory=set)     # empty = all categories
    difficulties: set = field(default_factory=set)    # empty = all difficulties
    word_delay: float = DEFAULT_WORD_DELAY


@dataclass
class QuizSession:
    tossup: dict
    words: list
    power_word_count: int
    message: discord.Message
    channel_id: int
    word_delay: float
    min_words_per_edit: int = 1
    max_words_per_edit: int = 1
    revealed_count: int = 0
    start_time: float = 0.0
    finished: bool = False
    task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Buzz state
    view: Optional["BuzzView"] = None
    paused: bool = False
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    buzzed_by: Optional[discord.abc.User] = None
    buzz_revealed_count: int = 0
    buzz_time: float = 0.0
    buzz_timeout_task: Optional[asyncio.Task] = None
    burned: set = field(default_factory=set)  


channel_settings: dict[int, ChannelSettings] = {}
active_sessions: dict[int, QuizSession] = {}
http_session: Optional[aiohttp.ClientSession] = None
backend = None 

def get_settings(channel_id: int) -> ChannelSettings:
    return channel_settings.setdefault(channel_id, ChannelSettings())

def parse_tossup_text(raw_question: str):
    """Strip HTML, split into words, and find the word index where the
    power mark ends, using qbreader/MODAQ's literal "(*)" convention."""
    plain = html.unescape(TAG_RE.sub("", raw_question)).strip()

    power_word_count = 0
    if POWER_MARK in plain:
        before, _, after = plain.partition(POWER_MARK)
        power_word_count = len(before.split())
        plain = f"{before.strip()} {after.strip()}".strip()

    words = plain.split()
    return words, power_word_count


def answer_plain_text(session: QuizSession) -> str:
    return html.unescape(TAG_RE.sub("", session.tossup.get("answer", "")))


def make_header(session: QuizSession) -> str:
    category = session.tossup.get("category", "Unknown")
    subcategory = session.tossup.get("subcategory", "")
    header = f" **Tossup**: *{category}"
    if subcategory and subcategory != category:
        header += f" / {subcategory}"
    header += "*\n\n_ _"
    return header



class BuzzView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Buzz", style=discord.ButtonStyle.danger)
    async def buzz_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = active_sessions.get(self.channel_id)
        if session is None or session.finished:
            await interaction.response.send_message("There's no round running here.", ephemeral=True)
            return

        async with session.lock:
            if interaction.user.id in session.burned:
                await interaction.response.send_message(
                    "You already tried this question — let someone else buzz.", ephemeral=True
                )
                return
            if session.paused:
                await interaction.response.send_message(
                    f"{session.buzzed_by.display_name if session.buzzed_by else 'Someone'} "
                    f"already buzzed — wait your turn.",
                    ephemeral=True,
                )
                return

            session.paused = True
            session.buzzed_by = interaction.user
            session.buzz_revealed_count = session.revealed_count
            session.buzz_time = time.monotonic()

        button.disabled = True
        button.label = f"🔴 {interaction.user.display_name} is answering…"
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

        await interaction.channel.send(
            f"🔴 **{interaction.user.display_name}** buzzed! You have {ANSWER_WINDOW:.0f}s — type your answer."
        )

        session.buzz_timeout_task = asyncio.create_task(buzz_timeout(session))

    def reset_button(self):
        btn = self.children[0]
        btn.disabled = False
        btn.label = "🔴 Buzz!"

    def lock_button(self, label: str = "Round over"):
        btn = self.children[0]
        btn.disabled = True
        btn.label = label


async def buzz_timeout(session: QuizSession):
    try:
        await asyncio.sleep(ANSWER_WINDOW)
    except asyncio.CancelledError:
        return

    async with session.lock:
        if session.finished or not session.paused:
            return  
        await resolve_buzz(session, correct=False, timed_out=True)


async def resolve_buzz(session: QuizSession, correct: bool, timed_out: bool = False):
    """Must be called while holding session.lock."""
    buzzer = session.buzzed_by

    if correct:
        await end_round_with_winner(session)
        return

    if buzzer:
        session.burned.add(buzzer.id)
        note = "⌛ Time's up!" if timed_out else "❌ Incorrect."
        await session.message.channel.send(f"{note} **{buzzer.display_name}** is out on this one. Resuming…")

    session.buzzed_by = None
    session.paused = False

    if session.view:
        session.view.reset_button()
        try:
            await session.message.edit(view=session.view)
        except discord.HTTPException:
            pass

    session.resume_event.set()


async def start_round(channel: discord.abc.Messageable, channel_id: int):
    if channel_id in active_sessions and not active_sessions[channel_id].finished:
        await channel.send("A round is already running here — buzz in or use `/qb stop`.")
        return

    settings = get_settings(channel_id)
    difficulties = set()
    for name in settings.difficulties:
        difficulties.update(backend.DIFFICULTY_TIERS.get(name, []))

    tossup = await backend.fetch_random_tossup(settings.categories, difficulties)
    if tossup is None:
        await channel.send(
            f"Couldn't get a tossup from the {backend.name} backend — try again in a moment, "
            "or broaden your filters. (This can happen if the source is momentarily unreachable.)"
        )
        return

    words, power_word_count = parse_tossup_text(tossup.get("question", ""))
    if not words:
        await channel.send("Got an empty question from the API, try again with `/qb start`.")
        return

    max_words_per_edit = max(1, round(TARGET_EDIT_INTERVAL / settings.word_delay))
    min_words_per_edit = max(1, max_words_per_edit // 2)

    session = QuizSession(
        tossup=tossup,
        words=words,
        power_word_count=power_word_count,
        message=None,
        channel_id=channel_id,
        word_delay=settings.word_delay,
        min_words_per_edit=min_words_per_edit,
        max_words_per_edit=max_words_per_edit,
    )

    view = BuzzView(channel_id)
    session.view = view
    message = await channel.send(make_header(session), view=view)
    session.message = message

    active_sessions[channel_id] = session
    session.task = asyncio.create_task(run_reveal(session))


async def wait_while_paused(session: QuizSession):
    while session.paused and not session.finished:
        session.resume_event.clear()
        try:
            await asyncio.wait_for(session.resume_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def run_reveal(session: QuizSession):
    header = make_header(session)
    session.start_time = time.monotonic()
    n = len(session.words)
    i = 0
    words_since_edit = 0
    try:
        while i < n:
            if session.finished:
                return
            if session.paused:
                await wait_while_paused(session)
                continue

            i += 1
            session.revealed_count = i
            words_since_edit += 1

            word = session.words[i - 1]
            at_clause_break = word.rstrip("\"'”’)]}").endswith(CLAUSE_END_CHARS)
            is_last_word = (i == n)

            should_edit = (
                is_last_word
                or words_since_edit >= session.max_words_per_edit
                or (at_clause_break and words_since_edit >= session.min_words_per_edit)
            )

            if should_edit:
                text = " ".join(session.words[:i])
                try:
                    await session.message.edit(content=f"{header}{text} ▌")
                except discord.HTTPException:
                    pass

                # Typing indicator lasts ~10s and is on a much looser rate
                # limit than message edits — use it to keep the channel
                # feeling "live" during the gap until the next edit.
                # NOTE: trigger_typing() was removed in discord.py 2.0; the
                # replacement is channel.typing() used as an async context
                # manager. Kept in its own try/except so a typing-indicator
                # failure can never kill the reveal task the way it did before.
                if not is_last_word:
                    try:
                        async with session.message.channel.typing():
                            pass
                    except (discord.HTTPException, AttributeError):
                        pass
                words_since_edit = 0

            slept = 0.0
            while slept < session.word_delay:
                if session.finished or session.paused:
                    break
                step = min(0.05, session.word_delay - slept)
                await asyncio.sleep(step)
                slept += step

        if session.finished:
            return

        full_text = " ".join(session.words)
        try:
            await session.message.edit(content=f"{header}{full_text}")
        except discord.HTTPException:
            pass

        elapsed = 0.0
        while elapsed < ANSWER_GRACE_PERIOD:
            if session.finished:
                return
            if session.paused:
                await wait_while_paused(session)
                continue
            step = 0.05
            await asyncio.sleep(step)
            elapsed += step

        async with session.lock:
            if session.finished:
                return
            session.finished = True
            if session.view:
                session.view.lock_button("Round over")
                try:
                    await session.message.edit(view=session.view)
                except discord.HTTPException:
                    pass
            channel = session.message.channel
            await channel.send(f"⏰ Time's up! The answer was: **{answer_plain_text(session)}**")
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[quizbowl_bot] run_reveal crashed for channel {session.channel_id}: {exc!r}")
        session.finished = True
        try:
            if session.view:
                session.view.lock_button("Round errored — see logs")
                await session.message.edit(view=session.view)
            await session.message.channel.send(
                "Something went wrong running that round and it had to stop. "
            )
        except discord.HTTPException:
            pass
    finally:
        active_sessions.pop(session.channel_id, None)


async def end_round_with_winner(session: QuizSession):
    session.finished = True
    if session.task and not session.task.done():
        session.task.cancel()

    author = session.buzzed_by
    elapsed = session.buzz_time - session.start_time
    revealed_at_buzz = session.buzz_revealed_count

    full_text = " ".join(session.words)
    in_power = session.power_word_count > 0 and revealed_at_buzz <= session.power_word_count
    points = 15 if in_power else 10
    power_note = " 🔥 **POWER!**" if in_power else ""

    header = make_header(session)
    if session.view:
        session.view.lock_button("Round over")
    try:
        await session.message.edit(content=f"{header}{full_text}", view=session.view)
    except discord.HTTPException:
        pass

    channel = session.message.channel
    await channel.send(
        f"✅ **{author.display_name}** got it! Buzzed in **{elapsed:.1f}s** "
        f"({revealed_at_buzz}/{len(session.words)} words in)!{power_note} "
        f"(+{points} pts)\nAnswer: **{answer_plain_text(session)}**"
    )
    active_sessions.pop(session.channel_id, None)


async def stop_round(channel: discord.abc.Messageable, channel_id: int):
    session = active_sessions.get(channel_id)
    if not session:
        await channel.send("There's no round running here.")
        return
    session.finished = True
    session.resume_event.set()
    if session.buzz_timeout_task and not session.buzz_timeout_task.done():
        session.buzz_timeout_task.cancel()
    if session.task and not session.task.done():
        session.task.cancel()
    if session.view:
        session.view.lock_button("Round stopped")
        try:
            await session.message.edit(view=session.view)
        except discord.HTTPException:
            pass
    active_sessions.pop(channel_id, None)
    await channel.send(f"🛑 Round stopped. The answer was: **{answer_plain_text(session)}**")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    session = active_sessions.get(message.channel.id)
    if (
        session
        and not session.finished
        and session.paused
        and session.buzzed_by is not None
        and message.author.id == session.buzzed_by.id
    ):
        async with session.lock:
            if session.finished or not session.paused or session.buzzed_by is None:
                return
            if message.author.id != session.buzzed_by.id:
                return

            result = await backend.check_answer(session.tossup.get("answer", ""), message.content)
            directive = result.get("directive", "reject")

            if directive == "accept":
                if session.buzz_timeout_task and not session.buzz_timeout_task.done():
                    session.buzz_timeout_task.cancel()
                await resolve_buzz(session, correct=True)
            elif directive == "prompt":
                prompt_text = result.get("directedPrompt") or "Can you be more specific?"
                await message.reply(f"🤔 {prompt_text}", mention_author=False)
                # buzz window keeps running; do not resolve
            else:
                if session.buzz_timeout_task and not session.buzz_timeout_task.done():
                    session.buzz_timeout_task.cancel()
                await resolve_buzz(session, correct=False)

    await bot.process_commands(message)


# ----------------------------------------------------------------------------
# UI: settings panel
# ----------------------------------------------------------------------------

class CategorySelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        options = [discord.SelectOption(label=cat, value=cat) for cat in backend.CATEGORIES]
        super().__init__(
            placeholder="Choose categories (none = all categories)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        settings = get_settings(self.channel_id)
        settings.categories = set(self.values)
        label = ", ".join(sorted(settings.categories)) if settings.categories else "All categories"
        await interaction.response.send_message(f"Categories set to: **{label}**", ephemeral=True)


class DifficultySelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        options = [discord.SelectOption(label=tier, value=tier) for tier in backend.DIFFICULTY_TIERS]
        super().__init__(
            placeholder="Choose difficulty (none = all difficulties)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        settings = get_settings(self.channel_id)
        settings.difficulties = set(self.values)
        label = ", ".join(sorted(settings.difficulties)) if settings.difficulties else "All difficulties"
        await interaction.response.send_message(f"Difficulty set to: **{label}**", ephemeral=True)


class SpeedSelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        options = [
            discord.SelectOption(label=f"{name} ({delay:.2f}s/word)", value=name)
            for name, delay in SPEED_PRESETS.items()
        ]
        super().__init__(placeholder="Reading speed (default: Normal)", min_values=1, max_values=1, options=options)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        get_settings(self.channel_id).word_delay = SPEED_PRESETS[name]
        await interaction.response.send_message(f"Reading speed set to **{name}**.", ephemeral=True)


class QuizPanel(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.add_item(CategorySelect(channel_id))
        self.add_item(DifficultySelect(channel_id))
        self.add_item(SpeedSelect(channel_id))

    @discord.ui.button(label="▶️ Start Round", style=discord.ButtonStyle.green, row=3)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Starting a tossup…", ephemeral=True)
        await start_round(interaction.channel, self.channel_id)

    @discord.ui.button(label="⏹️ Stop Round", style=discord.ButtonStyle.red, row=3)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await stop_round(interaction.channel, self.channel_id)
        await interaction.response.send_message("Round stopped.", ephemeral=True)


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------

qb_group = app_commands.Group(name="qb", description="Quiz bowl commands")


@qb_group.command(name="panel", description="Post the quiz bowl control panel")
async def panel_cmd(interaction: discord.Interaction):
    view = QuizPanel(interaction.channel_id)
    await interaction.response.send_message(
        "**Quiz Bowl Control Panel**\nPick categories/difficulty/speed below, then hit Start.",
        view=view,
    )


@qb_group.command(name="start", description="Start a quiz bowl tossup right now")
@app_commands.describe(categories="Comma-separated categories, e.g. 'Science,History' (optional)")
async def start_cmd(interaction: discord.Interaction, categories: str = None):
    if categories:
        chosen = {c.strip().title() for c in categories.split(",")}
        invalid = chosen - set(backend.CATEGORIES)
        if invalid:
            await interaction.response.send_message(
                f"Unknown categories: {', '.join(invalid)}. Use `/qb categories` to see valid names.",
                ephemeral=True,
            )
            return
        get_settings(interaction.channel_id).categories = chosen

    await interaction.response.send_message("Starting a tossup…")
    await start_round(interaction.channel, interaction.channel_id)


@qb_group.command(name="speed", description="Set how fast the question reads out in this channel")
@app_commands.choices(preset=[app_commands.Choice(name=name, value=name) for name in SPEED_PRESETS])
async def speed_cmd(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    get_settings(interaction.channel_id).word_delay = SPEED_PRESETS[preset.value]
    await interaction.response.send_message(
        f"Reading speed set to **{preset.value}** ({SPEED_PRESETS[preset.value]:.2f}s/word).", ephemeral=True
    )


@qb_group.command(name="stop", description="Stop the current quiz bowl round")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Stopping…", ephemeral=True)
    await stop_round(interaction.channel, interaction.channel_id)


@qb_group.command(name="categories", description="List valid category names")
async def categories_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"**Backend:** {backend.name}\n"
        "**Categories:** " + ", ".join(backend.CATEGORIES) + "\n"
        "**Difficulty tiers:** " + ", ".join(backend.DIFFICULTY_TIERS) + "\n"
        "**Speed presets:** " + ", ".join(SPEED_PRESETS),
        ephemeral=True,
    )


bot.tree.add_command(qb_group)


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------

@bot.event
async def on_ready():
    global http_session, backend
    if http_session is None:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
        http_session = aiohttp.ClientSession(timeout=timeout)
    if backend is None:
        backend_cls = get_backend_class(QUIZ_BACKEND)
        backend = backend_cls(http_session)
    await bot.tree.sync()
    print(f"Logged in as {bot.user} — quiz bowl bot ready, backend: {backend.name}")


if __name__ == "__main__":
    bot.run(TOKEN)
