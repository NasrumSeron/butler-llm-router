"""
goldset.py — the labelled messages Butler must route correctly.

22 hand-written messages, each labelled with the outcome that counts as correct.
They were drafted to cover each bot, the "no bot fits" case and known collisions;
they are NOT sampled from real traffic.

expect_service:
    "calendar" / "pc" / "islam"   the bot that must handle it
    None                          no bot fits; Butler answers it himself
    AMBIGUOUS                     Butler must ASK rather than guess (no such row
                                  at the moment — the clarify logic is covered by
                                  the offline tests instead)
    a tuple, e.g. ("calendar", None)
                                  any of these specific outcomes is acceptable

"pc" and "islam" are two further bots in my own deployment. They are not in this
repository, so reproducing the published score needs bots answering to those
names (their /capabilities decide what the router sees).

The four money rows expect None: a finance bot was retired, and these check
that Butler doesn't invent a bot for a money question.
"""

# Three outcomes, not two.
#
# AMBIGUOUS  Butler MUST stop and ask. Acting on its own is wrong even if it
#            happens to pick the bot you wanted.
# EITHER     More than one bot is a fine answer. Don't count it as a failure
#            whichever way it goes, and don't force a prompt for it.
#
# The distinction was added 2026-09-09: the runner was reporting sensible
# routes as failures purely because a message touched two domains, which made
# the score less useful than reading the rows.
AMBIGUOUS = "__ambiguous__"
EITHER = "__either__"

GOLD = [
    # --- clearly calendar ----------------------------------------------
    ("lunch with Sarah next Tuesday 1pm",                      "calendar"),
    ("add a meeting with the project team Thursday 3pm",        "calendar"),
    ("block out Friday morning for marking",                   "calendar"),
    ("reschedule tomorrow's appointment to 4pm",               "calendar"),
    ("remind me about the CPF contribution deadline",          "calendar"),  # G1
    ("set something up for the portfolio review next month",   "calendar"),  # G2
    ("remind me to read a hadith every morning",                "calendar"),  # K2 — time guard: islam can't schedule

    # --- clearly pc ------------------------------------------------------
    ("turn on my PC",                                          "pc"),
    ("is the computer still on?",                               "pc"),

    # --- clearly islam -----------------------------------------------------
    ("send me a hadith",                                       "islam"),
    ("give me one of the 99 names",                            "islam"),

    # --- no bot, money (ex-finance): Butler must not misroute these -----
    ("log my CPF top up of 500",                               None),
    ("what's my current investment portfolio worth",           None),
    ("record the insurance premium I paid today",              None),
    ("how much is left on my renovation loan",                 None),

    # --- no bot, general: Butler answers --------------------------------
    ("hello",                                                  None),
    ("what can you do",                                        None),
    ("what's the difference between an API and an SDK",        None),
    ("thanks, that worked",                                    None),
    ("what time is Maghrib today",                             None),  # G7 — islam excludes prayer times

    # --- pc can't schedule: time guard sends this to chat or calendar ----
    ("turn on my PC at 8am tomorrow",                           (None, "calendar")),  # K1

    # --- genuinely ambiguous: two bots each have a real claim -----------

    # --- calendar-or-chat: either is an acceptable answer ----------------
    ("when is my insurance premium due",                        ("calendar", None)),  # G3
]


def summary() -> str:
    from collections import Counter
    counts = Counter(
        ("tuple" if isinstance(expected, tuple) else (expected or "chat"))
        for _, expected in GOLD
    )
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
