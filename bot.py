import os
import json
import logging
import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
MY_TZ = "Asia/Singapore"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

COLOR_MAP: dict = json.loads(os.environ.get("COLOR_MAP", "{}"))

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

_calendar_service = None


# ---------------------------------------------------------------------------
# Google Calendar auth
# ---------------------------------------------------------------------------

def get_calendar_service():
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service

    creds = None
    token_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open("token.json", "w") as f:
                f.write(creds.to_json())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as f:
                f.write(creds.to_json())

    _calendar_service = build("calendar", "v3", credentials=creds)
    return _calendar_service


# ---------------------------------------------------------------------------
# Claude intent parsing
# ---------------------------------------------------------------------------

def classify_and_extract(text: str, pending: Optional[dict]) -> dict:
    now = datetime.datetime.now(ZoneInfo(MY_TZ))
    pending_ctx = ""
    if pending:
        if pending["action"] == "conflict_resolve":
            pending_ctx = (
                f"\nContext: there is a pending conflict resolution for a new event "
                f"'{pending['new_title']}' awaiting user confirmation (replace or keep)."
            )
        else:
            pending_ctx = (
                f"\nContext: there is a pending '{pending['action']}' for event "
                f"'{pending['event_summary']}' at {pending['event_time']} awaiting user confirmation."
            )

    system = f"""You are a calendar assistant. Current datetime: {now.isoformat()} (Asia/Singapore, UTC+08:00).{pending_ctx}

Classify the user message and return ONLY a valid JSON object — no prose, no markdown fences — with exactly these keys:

  intent        : "create" | "edit" | "delete" | "confirm" | "cancel" | "list" | "search" | "other"
  title         : string or null   — event title for create
  start         : ISO 8601 with +08:00 offset, or null
  end           : ISO 8601 with +08:00 offset, or null (default: start + 1 hour)
  all_day       : boolean
  search_title  : string or null   — title fragment to find an existing event (for edit/delete)
  search_date   : "YYYY-MM-DD" or null   — date hint to narrow the search
  updates       : object or null   — for edit: keys are any of title/start/end with new values
  query_date    : "YYYY-MM-DD" or null   — for list: the date the user wants to see
  search_query  : string or null   — for search: keyword to find upcoming events by name
  category      : string or null   — best matching category from this list: {list(COLOR_MAP.keys())}

Rules:
- Resolve relative dates ("tomorrow", "next Friday", "this evening") from the current datetime.
- All datetimes must include Singapore +08:00 offset.
- "yes", "confirm", "ok", "sure", "proceed", "do it", "replace" → intent: "confirm"
- "no", "cancel", "never mind", "stop", "don't", "keep" → intent: "cancel"
- Editing just a time: put the new datetime in updates.start; if only time-of-day is given, keep the same date as the event being edited (use search_date as a reference).
- Multi-day events ("trip from June 20 to June 25", "holiday 1–5 July"): set all_day: true, start to the first day, end to the LAST INCLUSIVE day (the code will handle the exclusive offset). Use "YYYY-MM-DD" format for all_day start/end.
- Single words like "trip", "holiday", "travel", "vacation", "camp" with a date range always imply all_day: true.
- "when is my [event]", "when do I have [event]", "when is [event] due", "next [event]" → intent: "search", search_query: the event keyword. Use this when the user mentions a specific event NAME and wants to find it.
- "what do I have on [date]", "what's on [date]", "show me [date]", "am I free on [date]" → intent: "list", query_date: that date. Use this ONLY when a specific date is mentioned, not an event name.
- IMPORTANT: "when is my X" always means search for event X, never list.
- If the message is not calendar-related, return intent: "other"."""

    resp = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def fmt_time(event: dict) -> str:
    start = event.get("start", {})
    if "dateTime" in start:
        dt = datetime.datetime.fromisoformat(start["dateTime"]).astimezone(ZoneInfo(MY_TZ))
        return dt.strftime("%a %d %b %Y, %I:%M %p SGT")
    if "date" in start:
        return f"{start['date']} (all day)"
    return "unknown time"


def check_conflicts(service, start_dt: datetime.datetime, end_dt: datetime.datetime, exclude_id: str = None, exclude_event: dict = None) -> list:
    result = service.events().list(
        calendarId="primary",
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = result.get("items", [])
    if exclude_id:
        events = [e for e in events if e["id"] != exclude_id]
    # Fallback: also exclude by matching summary + original start, in case IDs differ
    if exclude_event:
        orig_start = exclude_event.get("start", {})
        events = [
            e for e in events
            if not (e.get("summary") == exclude_event.get("summary") and e.get("start") == orig_start)
        ]
    return events


def find_events(service, title_hint: str, date_hint: Optional[str] = None) -> list:
    if date_hint:
        base = datetime.datetime.strptime(date_hint, "%Y-%m-%d").replace(tzinfo=ZoneInfo(MY_TZ))
        time_min = base.isoformat()
        time_max = (base + datetime.timedelta(days=1)).isoformat()
    else:
        now = datetime.datetime.now(ZoneInfo(MY_TZ))
        time_min = now.isoformat()
        time_max = (now + datetime.timedelta(days=30)).isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        q=title_hint,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def get_events_for_date(service, date: datetime.date) -> list:
    start = datetime.datetime.combine(date, datetime.time.min).replace(tzinfo=ZoneInfo(MY_TZ))
    end = datetime.datetime.combine(date, datetime.time.max).replace(tzinfo=ZoneInfo(MY_TZ))

    all_events = []
    cal_list = service.calendarList().list().execute()
    for cal in cal_list.get("items", []):
        try:
            result = service.events().list(
                calendarId=cal["id"],
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
            ).execute()
            all_events.extend(result.get("items", []))
        except Exception:
            pass  # skip calendars we can't read

    all_events.sort(
        key=lambda e: e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""
    )

    # Deduplicate by (summary, start) across calendars
    seen = set()
    unique = []
    for e in all_events:
        key = (e.get("summary", ""), e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"))
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def format_day_summary(events: list, label: str) -> str:
    if not events:
        return f"You have nothing scheduled {label}."
    lines = []
    for e in events:
        start = e.get("start", {})
        end = e.get("end", {})
        if "dateTime" in start:
            start_dt = datetime.datetime.fromisoformat(start["dateTime"]).astimezone(ZoneInfo(MY_TZ))
            end_dt = datetime.datetime.fromisoformat(end["dateTime"]).astimezone(ZoneInfo(MY_TZ))
            time_str = f"{start_dt.strftime('%I:%M %p').lstrip('0')} – {end_dt.strftime('%I:%M %p').lstrip('0')}"
        else:
            time_str = "All day"
        lines.append(f"🔹 {time_str} — {e['summary']}")
    return f"{label.capitalize()}:\n" + "\n".join(lines)


def search_events(service, query: str, days: int = 90) -> list:
    now = datetime.datetime.now(ZoneInfo(MY_TZ))
    time_max = (now + datetime.timedelta(days=days)).isoformat()
    all_events = []
    cal_list = service.calendarList().list().execute()
    for cal in cal_list.get("items", []):
        try:
            result = service.events().list(
                calendarId=cal["id"],
                timeMin=now.isoformat(),
                timeMax=time_max,
                q=query,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            all_events.extend(result.get("items", []))
        except Exception:
            pass
    all_events.sort(
        key=lambda e: e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""
    )
    # Deduplicate
    seen = set()
    unique = []
    for e in all_events:
        key = (e.get("summary", ""), e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"))
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def conflict_text(conflicts: list) -> str:
    lines = "\n".join(f"  • {e['summary']} — {fmt_time(e)}" for e in conflicts)
    return f"There's already something scheduled at that time:\n{lines}\n\nPick a different time slot."


def color_id(category: Optional[str]) -> Optional[str]:
    if not category or not COLOR_MAP:
        return None
    return COLOR_MAP.get(category.lower().strip())


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hey! I'm your personal calendar bot.\n\n"
        "Here's what I can do:\n"
        '  ✅ "dentist tomorrow 3pm"\n'
        '  ✏️ "move dentist to 4pm"\n'
        '  🗑️ "delete gym session Friday"\n'
        '  📋 "what do I have on Monday?"\n'
        '  🌈 Events are colour-coded by category\n\n'
        "All times are in Singapore time (SGT)."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return

    text = update.message.text.strip()
    await update.message.chat.send_action("typing")
    logger.info("message: %r", text)

    pending: Optional[dict] = context.user_data.get("pending_action")

    try:
        parsed = classify_and_extract(text, pending)
    except Exception as e:
        logger.exception("classify_and_extract failed")
        await update.message.reply_text(f"⚠️ Could not parse that: {e}")
        return

    intent = parsed.get("intent", "other")
    logger.info("intent=%s category=%s", intent, parsed.get("category"))

    try:
        service = get_calendar_service()
    except Exception as e:
        logger.exception("get_calendar_service failed")
        await update.message.reply_text(f"⚠️ Google Calendar auth failed: {e}")
        return

    # ── CONFIRM ─────────────────────────────────────────────────────────────
    if intent == "confirm":
        if not pending:
            await update.message.reply_text("🤷 Nothing waiting for confirmation.")
            return

        if pending["action"] == "delete":
            service.events().delete(calendarId="primary", eventId=pending["event_id"]).execute()
            context.user_data.pop("pending_action", None)
            await update.message.reply_text(
                f"🗑️ Deleted: {pending['event_summary']} ({pending['event_time']})"
            )

        elif pending["action"] == "edit":
            service.events().patch(
                calendarId="primary",
                eventId=pending["event_id"],
                body=pending["patch"],
            ).execute()
            context.user_data.pop("pending_action", None)
            await update.message.reply_text(f"✅ Updated: {pending['event_summary']}")

        elif pending["action"] == "conflict_resolve":
            for eid in pending["conflicting_ids"]:
                service.events().delete(calendarId="primary", eventId=eid).execute()
            event = service.events().insert(calendarId="primary", body=pending["new_body"]).execute()
            context.user_data.pop("pending_action", None)
            await update.message.reply_text(
                f"🔄 Replaced. Added: {pending['new_title']}\n📅 {fmt_time(event)}\n🔗 {event.get('htmlLink', '')}"
            )

        return

    # ── CANCEL ───────────────────────────────────────────────────────────────
    if intent == "cancel":
        if pending:
            action = pending["action"]
            context.user_data.pop("pending_action", None)
            if action == "delete":
                await update.message.reply_text(f"👍 Got it, {pending['event_summary']} was not deleted.")
            elif action == "conflict_resolve":
                await update.message.reply_text("👍 Got it, no changes made.")
            else:
                await update.message.reply_text("👍 Got it, no changes made.")
        else:
            await update.message.reply_text("🤷 Nothing to cancel.")
        return

    # ── CREATE ───────────────────────────────────────────────────────────────
    if intent == "create":
        start_str = parsed.get("start")
        end_str = parsed.get("end")
        all_day = parsed.get("all_day", False)
        title = parsed.get("title") or "Untitled"

        if not start_str:
            await update.message.reply_text("🤔 I couldn't work out the date/time. Could you be more specific?")
            return

        cid = color_id(parsed.get("category"))

        if not all_day:
            start_dt = datetime.datetime.fromisoformat(start_str)
            end_dt = datetime.datetime.fromisoformat(end_str) if end_str else start_dt + datetime.timedelta(hours=1)
            conflicts = check_conflicts(service, start_dt, end_dt)
            if conflicts:
                conflict_names = "\n".join(f"  • {e['summary']} — {fmt_time(e)}" for e in conflicts)
                new_body = {
                    "summary": title,
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": MY_TZ},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": MY_TZ},
                    "reminders": {"useDefault": False, "overrides": []},
                }
                if cid:
                    new_body["colorId"] = cid
                context.user_data["pending_action"] = {
                    "action": "conflict_resolve",
                    "conflicting_ids": [e["id"] for e in conflicts],
                    "new_body": new_body,
                    "new_title": title,
                }
                await update.message.reply_text(
                    f"⚠️ There's already something scheduled at that time:\n{conflict_names}\n\n"
                    f"Reply 'replace' to replace it with '{title}', or 'keep' to leave it as is."
                )
                return
            body = {
                "summary": title,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": MY_TZ},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": MY_TZ},
                "reminders": {"useDefault": False, "overrides": []},
            }
        else:
            end_date = datetime.date.fromisoformat((end_str or start_str)[:10])
            end_date_exclusive = (end_date + datetime.timedelta(days=1)).isoformat()
            body = {
                "summary": title,
                "start": {"date": start_str[:10]},
                "end": {"date": end_date_exclusive},
                "reminders": {"useDefault": False, "overrides": []},
            }

        if cid:
            body["colorId"] = cid

        event = service.events().insert(calendarId="primary", body=body).execute()
        await update.message.reply_text(
            f"✅ Added: {title}\n📅 {fmt_time(event)}\n🔗 {event.get('htmlLink', '')}"
        )
        return

    # ── EDIT ─────────────────────────────────────────────────────────────────
    if intent == "edit":
        search_title = parsed.get("search_title") or parsed.get("title")
        if not search_title:
            await update.message.reply_text("🤔 Which event do you want to edit?")
            return

        matches = find_events(service, search_title, parsed.get("search_date"))
        if not matches:
            await update.message.reply_text(f"🔍 No upcoming event found matching '{search_title}'.")
            return

        event = matches[0]
        updates = parsed.get("updates") or {}
        patch: dict = {}

        if updates.get("title"):
            patch["summary"] = updates["title"]

        if updates.get("start"):
            new_start_dt = datetime.datetime.fromisoformat(updates["start"])

            # Preserve original duration if no new end given
            if updates.get("end"):
                new_end_dt = datetime.datetime.fromisoformat(updates["end"])
            else:
                orig_start = event.get("start", {}).get("dateTime")
                orig_end = event.get("end", {}).get("dateTime")
                if orig_start and orig_end:
                    duration = (
                        datetime.datetime.fromisoformat(orig_end)
                        - datetime.datetime.fromisoformat(orig_start)
                    )
                    new_end_dt = new_start_dt + duration
                else:
                    new_end_dt = new_start_dt + datetime.timedelta(hours=1)

            conflicts = check_conflicts(service, new_start_dt, new_end_dt, exclude_id=event["id"], exclude_event=event)
            if conflicts:
                conflict_names = "\n".join(f"  • {e['summary']} — {fmt_time(e)}" for e in conflicts)
                await update.message.reply_text(
                    f"⚠️ That time conflicts with:\n{conflict_names}\n\nPick a different time."
                )
                return

            patch["start"] = {"dateTime": new_start_dt.isoformat(), "timeZone": MY_TZ}
            patch["end"] = {"dateTime": new_end_dt.isoformat(), "timeZone": MY_TZ}

        if not patch:
            await update.message.reply_text("🤔 What would you like to change about that event?")
            return

        updated = service.events().patch(calendarId="primary", eventId=event["id"], body=patch).execute()
        await update.message.reply_text(f"✅ Updated: {event['summary']}\n📅 {fmt_time(updated)}")
        return

    # ── DELETE ────────────────────────────────────────────────────────────────
    if intent == "delete":
        search_title = parsed.get("search_title") or parsed.get("title")
        if not search_title:
            await update.message.reply_text("🤔 Which event do you want to delete?")
            return

        matches = find_events(service, search_title, parsed.get("search_date"))
        if not matches:
            await update.message.reply_text(f"🔍 No upcoming event found matching '{search_title}'.")
            return

        event = matches[0]
        event_time = fmt_time(event)
        context.user_data["pending_action"] = {
            "action": "delete",
            "event_id": event["id"],
            "event_summary": event["summary"],
            "event_time": event_time,
        }
        await update.message.reply_text(
            f"🗑️ Delete '{event['summary']}' ({event_time})?\n\nReply 'yes' to confirm or 'no' to keep it."
        )
        return

    # ── LIST ──────────────────────────────────────────────────────────────────
    if intent == "list":
        query_date_str = parsed.get("query_date")
        if not query_date_str:
            # Misclassified — treat as search using the original text
            intent = "search"
            parsed["search_query"] = parsed.get("search_query") or parsed.get("title") or text
        else:
            date = datetime.date.fromisoformat(query_date_str)
            events = get_events_for_date(service, date)
            label = date.strftime("%A %d %b")
            await update.message.reply_text(format_day_summary(events, f"on {label}"))
            return

    # ── SEARCH ────────────────────────────────────────────────────────────────
    if intent == "search":
        query = parsed.get("search_query")
        if not query:
            await update.message.reply_text("🤔 What event are you looking for?")
            return
        matches = search_events(service, query)
        if not matches:
            await update.message.reply_text(f"🔍 No upcoming events found matching '{query}'.")
            return
        lines = [f"🔍 Upcoming '{query}' events:\n"]
        for e in matches[:10]:
            lines.append(f"🔹 {fmt_time(e)} — {e['summary']}")
        await update.message.reply_text("\n".join(lines))
        return

    # ── OTHER ─────────────────────────────────────────────────────────────────
    await update.message.reply_text(
        "🤖 I manage your calendar. Try:\n"
        '  "team meeting Friday 10am–11am"\n'
        '  "change dentist to 4:30pm"\n'
        '  "delete gym session Monday"\n'
        '  "what do I have on Friday?"'
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    get_calendar_service()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


    if WEBHOOK_URL:
        # Production: Telegram pushes updates to your URL
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8080)),
            url_path="/webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
        )
    else:
        # Local dev: bot polls Telegram
        app.run_polling()


if __name__ == "__main__":
    main()
