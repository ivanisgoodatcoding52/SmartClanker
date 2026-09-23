# Quiz Bowl Discord Bot

A Discord bot that runs live quiz bowl tossups: reads the question out word by
word, lets players buzz in with a button, and scores power/regular tossups
Tournament-style.

## Features:
- Speed adjustment
- Different Catagories
- Scoreboard
- and more!

Question source is pluggable. Default backend is qbreader.org.
See backends/base.py for the interface, and README.md for how to
add or switch backends via the QUIZ_BACKEND environment variable.

Slash commands:
  /qb panel                - posts an interactive panel (categories + difficulty + speed + Start/Stop)
  /qb start [categories]   - instantly starts a tossup (optional comma-separated categories)
  /qb speed <preset>       - sets how fast the question reads out in this channel
  /qb stop                 - stops the round currently running in this channel
  /qb categories           - lists valid category names

How a round works:
  1. The bot pulls a random tossup from qbreader filtered by your chosen categories.
  2. It reveals the question word-by-word in one message (like a real moderator reading it).
  3. Anyone can hit the 🔴 Buzz button to interrupt the reading. Once you buzz, the reveal
     freezes, everyone else is locked out, and you have a set window (ANSWER_WINDOW seconds)
     to type your answer in chat.
       - Correct  -> you win the tossup.
       - Wrong / time runs out -> you're "burned" for this question (can't buzz again on it),
         and the reading resumes from where it paused for everyone else.
  4. Buzzing before the power mark (qbreader/MODAQ-style literal "(*)" in the question text,
     when the packet has one) is worth 15 points instead of 10, same as real quiz bowl.
  5. If nobody gets it, the full question + answer reveal automatically once the clock runs out.
