# The Butler Service Contract v1.1

Every bot Butler manages is an independent Docker container that speaks HTTP on the
private Docker network. Butler never imports another bot's code and never touches its
database. It only calls these three endpoints.

If a bot implements these three endpoints, Butler can manage it. That is the whole rule.

---

## 1. `GET /health`

"Are you alive?" Butler calls this at startup and before every invoke.

**Response 200:**

```json
{ "ok": true, "name": "calendar", "version": "2.0" }
```

Anything other than a 200 with `ok: true` means the bot is DOWN. Butler will tell the user
"Calendar bot isn't responding" rather than silently failing.

---

## 2. `GET /capabilities`

"What can you do?" Butler calls this at startup and caches the answer. **This is the
endpoint that means Butler's code never changes when you add a new bot.**

**Response 200:**

```json
{
  "name": "calendar",
  "description": "Creates calendar events on the user's iCloud calendars from natural language.",
  "keywords": ["calendar", "event", "meeting", "appointment", "schedule", "remind me on"],
  "actions": [
    {
      "name": "create_event",
      "description": "Create a calendar event from a natural-language description.",
      "params": {
        "text": "The user's original message, verbatim."
      }
    }
  ]
}
```

Field by field:

| Field | Who uses it | Why it matters |
|---|---|---|
| `name` | Butler | Must match the key in `services.yaml`. |
| `description` | The routing LLM | This is the sales pitch. A vague description causes misrouting. Write it as "does X, does NOT do Y". |
| `keywords` | The keyword fast-path | Lowercase substrings. A match here skips the LLM entirely — free and instant. |
| `actions[].name` | Butler | Passed back in `/invoke`. |
| `actions[].description` | The routing LLM | How it picks between two actions in the same bot. |
| `actions[].params` | The routing LLM | Object of `param_name -> plain-English description of what to put there`. |

---

## 3. `POST /invoke`

"Do this." The only endpoint that changes state.

**Request body:**

```json
{
  "action": "create_event",
  "params": { "text": "lunch with Sarah next Tuesday 1pm" },
  "request_id": "b1f3c8a2",
  "user_id": 123456789,
  "original_message": "lunch with Sarah next Tuesday 1pm"
}
```

`original_message` is always included even when the action takes no params, so a bot can
fall back to parsing it itself.

**Response 200 — success:**

```json
{
  "ok": true,
  "reply": "Added *Lunch with Sarah* on Tue 15 Sep, 1:00pm to your Personal calendar.",
  "data": { "event_uid": "..." }
}
```

**Response 200 — the bot ran but could not do it:**

```json
{
  "ok": false,
  "error": "Could not work out a date from that message."
}
```

Rules:

- `reply` is **Telegram Markdown** and is shown to the user verbatim. The bot owns its own
  wording; Butler does not rewrite it, and no LLM sits between the bot and the screen.
- A bot must respond within `INVOKE_TIMEOUT_SECONDS` (default 25) or Butler reports a timeout.
- `data` is optional and is for Butler's logs and future chaining. The user never sees it.

---

## 4a. `followup` — holding the conversation (added v1.1)

Some jobs need more than one exchange. The calendar bot shows you a draft, you
type "make it 2pm", you tap Add. Butler cannot route "make it 2pm" — it is
meaningless without the draft. So the bot asks to receive your next message.

Add a `followup` object to a successful `/invoke` response:

```json
{
  "ok": true,
  "reply": "```\nTitle:  Lunch with Sarah\n...\n```",
  "data": { "draft_id": "a1b2c3d4" },
  "followup": {
    "action": "amend_draft",
    "params": { "draft_id": "a1b2c3d4" },
    "expires_in": 600,
    "buttons": [
      { "label": "Add to calendar", "action": "confirm_event",
        "params": { "draft_id": "a1b2c3d4" } },
      { "label": "Cancel", "action": "cancel_draft",
        "params": { "draft_id": "a1b2c3d4" } }
    ]
  }
}
```

What Butler then does:

- Shows `reply`, with `buttons` underneath.
- **The user's next typed message goes straight to `action`**, with `params`
  merged in and any remaining declared param filled with what they typed. No
  routing, no LLM call, no cost.
- A button tap invokes that button's `action` with its `params`.
- The lock drops the moment a response comes back **without** a `followup` — so
  ending the conversation means simply omitting the field.

Guarantees Butler makes, so a bot cannot trap the user:

| Situation | What happens |
|---|---|
| Bot returns `ok:false` | Lock released immediately |
| `expires_in` elapses (default 600s) | Lock released, routing resumes |
| User types `/cancel` | Lock released |
| User types any other `/command` | Breaks out to normal handling |

**State lives in the bot, not in Butler.** Butler carries an opaque
`draft_id` back and forth and never looks inside it. That is what keeps domain
logic out of Butler.

## 4b. `internal` actions

Mark an action `"internal": true` when it only makes sense as a follow-up:

```json
{ "name": "confirm_event", "internal": true,
  "description": "Write an open draft to iCloud.",
  "params": { "draft_id": "The draft to write." } }
```

Internal actions are **hidden from the routing model and from `/bots`**, but are
still fully invokable via `followup`. This matters more than it sounds: the
calendar bot has six actions and only one — `draft_event` — is something a
person ever asks for. Showing the routing model all six is how you teach it to
misroute. A bot with *one* public action also keeps the free keyword fast-path
working.

A bot whose actions are *all* internal is rejected at discovery — nothing could
ever reach it.

## 5. What a bot must NOT do

- Must not require inbound internet access. Docker-internal only.
- Must not assume Butler is the only caller — it may still have its own Telegram front door.
- Must not return content that itself reads as an instruction to Butler. Butler treats
  every `reply` as text to display, never as a command to act on.

---

## 6. Adding a new bot to Butler

1. Implement the three endpoints in the new bot.
2. Add four lines to `services.yaml` — and to `services.docker.yaml`, which is the
   one that runs in Docker. Two files, because a container cannot reach
   `127.0.0.1:9101`; on the shared network it addresses bots by container name.
3. Add the container to `docker-compose.yml` on the same network.
4. **Rebuild Butler — a restart is not enough.**

   ```bash
   docker compose up -d --build butler
   ```

   `services.docker.yaml` is `COPY`'d into the image, so a `docker restart` brings
   back the container with the OLD address book and the new bot simply is not
   there. This said "Restart Butler" until 22 Sep 2026 and cost an hour of
   wondering why a correctly-configured bot was invisible.

No Butler source file is edited. That is the test of whether this contract is
doing its job.
