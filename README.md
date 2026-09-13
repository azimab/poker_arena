# poker-arena

A competitive arena for heads-up no-limit Hold'em bots.

Players sign in with GitHub and upload a Python bot. Each submission runs in
an isolated sandbox and must survive short matches against built-in bots
before it goes live. Every account has one active bot at a time.

Active bots play a nightly round-robin, with seats swapped so neither side
gets the better cards. Results update each bot's skill rating, and the
leaderboard ranks bots by that rating.
