#!/usr/bin/env python3
"""
Intercom Daily Customer Support Report

Runs on a schedule (default: 08:00 every morning).
1. Pulls yesterday's conversations from Intercom (chats + emails).
2. Builds a fully structured analytics report in pure Python.
3. Emails the formatted HTML report to the configured recipients.

No external AI API needed — the report logic lives here.

Environment Variables (see .env.example):
    INTERCOM_API_TOKEN      - Intercom access token
    REPORT_RECIPIENTS       - Comma-separated email addresses for the report
    REPORT_SEND_TIME        - HH:MM in 24-hour format (default 08:00)
    SMTP_HOST / SMTP_PORT   - SMTP server settings
    EMAIL_1_ADDRESS         - Sender address
    EMAIL_1_PASSWORD        - App password for sender
    EMAIL_1_DISPLAY_NAME    - Display name for sender
"""

import os
import sys
import logging
import smtplib
import time
import schedule
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Optional

import requests

# Load .env from same directory
try:
    from dotenv import load_dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env):
        load_dotenv(_env, override=True)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("intercom-daily-report")

# ─── Config ───────────────────────────────────────────────────────────────────

INTERCOM_API_TOKEN: str = os.getenv("INTERCOM_API_TOKEN", "")
REPORT_RECIPIENTS: List[str] = [
    r.strip() for r in os.getenv("REPORT_RECIPIENTS", "damilola@ftuk.com").split(",") if r.strip()
]
REPORT_CC: List[str] = [
    r.strip() for r in os.getenv("REPORT_CC", "abdel@ftuk.com").split(",") if r.strip()
]
REPORT_BCC: List[str] = [
    r.strip() for r in os.getenv("REPORT_BCC", "").split(",") if r.strip()
]
REPORT_SEND_TIME: str = os.getenv("REPORT_SEND_TIME", "08:00")

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SENDER_ADDRESS: str = os.getenv("EMAIL_1_ADDRESS", "")
SENDER_PASSWORD: str = os.getenv("EMAIL_1_PASSWORD", "").replace(" ", "")
SENDER_DISPLAY: str = os.getenv("EMAIL_1_DISPLAY_NAME", "FTUK Support")

INTERCOM_API_BASE = "https://api.intercom.io"
INTERCOM_APP_ID: str = os.getenv("INTERCOM_APP_ID", "lhjtrulf")

# Known agents — fallback map (updated by fetch_admins at runtime)
ADMIN_MAP: Dict[str, str] = {
    "8770830": "Nick Quinn",
    "8770831": "FTUK's Assistant",
    "8889691": "Dami",
    "9022355": "ReZa",
    "9023402": "Umar",
    "9030249": "Tom",
    "10383433": "Bradley",
    "10384010": "James",
    "10390175": "Navin",
}

# Team map — id -> name
TEAM_MAP: Dict[str, str] = {
    "8892866": "Support",
    "8892873": "Payment/Order Issue",
    "8892885": "Incoming Emails",
    "8892896": "Level 2 Support",
    "8892904": "Partnerships & Affiliates",
    "8999577": "Payout",
    "9078615": "Unread Email",
    "9078621": "Responded Email",
}

# Escalation tag IDs
ESCALATION_TAG_IDS = {"13126646"}  # "Escalated emails"
ESCALATION_TAG_NAME = "Escalated emails"

# FIN bot identifiers — any admin name containing these strings is FIN
FIN_KEYWORDS = ["fin", "bot", "assistant", "automated", "resolution bot"]

# ─── Intercom helpers ─────────────────────────────────────────────────────────

def _intercom_headers() -> dict:
    return {
        "Authorization": f"Bearer {INTERCOM_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": "2.11",
    }


def fetch_admins() -> None:
    """Refresh ADMIN_MAP from the live Intercom workspace."""
    global ADMIN_MAP
    try:
        resp = requests.get(
            f"{INTERCOM_API_BASE}/admins",
            headers=_intercom_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        for a in resp.json().get("admins", []):
            ADMIN_MAP[str(a["id"])] = a.get("name", f"Agent {a['id']}")
        logger.info(f"Loaded {len(ADMIN_MAP)} admins from Intercom")
    except Exception as exc:
        logger.warning(f"Could not refresh admin list: {exc} — using fallback map")


def fetch_conversations_for_date(target_date: datetime) -> List[Dict[str, Any]]:
    """
    Fetch all conversations that were created or updated on target_date (UTC).
    Uses the Intercom search conversations endpoint.
    """
    start_ts = int(target_date.replace(hour=0, minute=0, second=0, microsecond=0,
                                        tzinfo=timezone.utc).timestamp())
    end_ts = int(target_date.replace(hour=23, minute=59, second=59, microsecond=999999,
                                      tzinfo=timezone.utc).timestamp())

    conversations: List[Dict[str, Any]] = []
    starting_after: Optional[str] = None

    while True:
        body: Dict[str, Any] = {
            "query": {
                "operator": "AND",
                "value": [
                    {"field": "updated_at", "operator": ">", "value": start_ts},
                    {"field": "updated_at", "operator": "<", "value": end_ts},
                ],
            },
            "pagination": {"per_page": 150},
        }
        if starting_after:
            body["pagination"]["starting_after"] = starting_after

        try:
            resp = requests.post(
                f"{INTERCOM_API_BASE}/conversations/search",
                headers=_intercom_headers(),
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error(f"Intercom API error fetching conversations: {exc}")
            break

        items = data.get("conversations", [])
        conversations.extend(items)
        logger.info(f"Fetched {len(items)} conversations (total so far: {len(conversations)})")

        pages = data.get("pages", {})
        next_page = pages.get("next", {})
        starting_after = next_page.get("starting_after") if next_page else None
        if not starting_after:
            break

    return conversations


def summarise_conversation(conv: Dict[str, Any]) -> Dict[str, Any]:
    """Extract key fields from a raw conversation object."""
    source = conv.get("source", {})
    stats  = conv.get("statistics", {}) or {}
    assignee = conv.get("assignee", {}) or {}
    team_assignee = conv.get("team_assignee", {}) or {}
    rating_obj = conv.get("conversation_rating") or {}
    ai_agent = conv.get("ai_agent") or {}
    teammates = conv.get("teammates", {}) or {}

    raw_type = source.get("type", "unknown")
    _TYPE_MAP = {"conversation": "chat", "email": "email", "admin_initiated": "admin_initiated"}
    channel = _TYPE_MAP.get(raw_type, raw_type)

    assignee_name = (assignee.get("name") or "") if assignee else ""

    tag_objs = conv.get("tags", {}).get("tags", []) or []
    tag_names = [t.get("name", "") for t in tag_objs]
    tag_ids   = {str(t.get("id", "")) for t in tag_objs}
    is_escalated = bool(ESCALATION_TAG_IDS & tag_ids)

    # teammates who participated (excluding FIN)
    teammate_ids = [str(a.get("id", "")) for a in (teammates.get("admins") or [])]

    # CSAT rating (1-5 or null)
    csat = rating_obj.get("rating") if rating_obj else None
    csat_teammate_id = str((rating_obj.get("teammate") or {}).get("id", ""))

    closed_by_id = str(stats.get("last_closed_by_id") or "")
    team_id      = str(team_assignee.get("id") or conv.get("team_assignee_id") or "")

    return {
        "id": conv.get("id"),
        "type": channel,
        "subject": source.get("subject", ""),
        "state": conv.get("state", "unknown"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "assignee_name": assignee_name if assignee_name else "Unassigned",
        "assignee_id": str(assignee.get("id") or ""),
        "team_id": team_id,
        "was_handled": bool(stats.get("last_closed_by_id") or stats.get("first_admin_reply_at")),
        "closed_by_id": closed_by_id,
        "time_to_first_response": stats.get("time_to_admin_reply"),
        "time_to_assignment": stats.get("time_to_assignment"),
        "time_to_close": stats.get("time_to_first_close"),
        "time_to_last_close": stats.get("time_to_last_close"),
        "median_time_to_reply": stats.get("median_time_to_reply"),
        "reopened_count": stats.get("count_reopens", 0),
        "reply_count": stats.get("count_conversation_parts", 0),
        "count_assignments": stats.get("count_assignments", 0),
        "tags": tag_names,
        "tag_ids": tag_ids,
        "is_escalated": is_escalated,
        "priority": conv.get("priority", "not_priority"),
        "read": conv.get("read", False),
        "first_message_preview": (source.get("body") or "")[:300],
        "csat": csat,
        "csat_teammate_id": csat_teammate_id,
        "teammate_ids": teammate_ids,
        "ai_agent_participated": bool(conv.get("ai_agent_participated")),
        "ai_agent_resolution": (ai_agent.get("resolution_state") or "") if ai_agent else "",
    }


# ─── Analytics report builder (pure Python) ───────────────────────────────────

# Keywords used to auto-categorise conversations by subject / preview
def _trimmed_avg(values: List[float], trim: int = 2) -> Optional[float]:
    """Return mean after removing `trim` lowest and `trim` highest outliers.
    Falls back to plain mean when there are not enough values to trim."""
    if not values:
        return None
    s = sorted(values)
    if len(s) > trim * 2:
        s = s[trim:-trim]
    return sum(s) / len(s)

CATEGORIES: List[tuple] = [
    ("Billing & Payments",     ["billing", "invoice", "payment", "charge", "refund", "subscription", "fee"]),
    ("Payouts & Withdrawals",  ["payout", "withdrawal", "withdraw", "transfer", "funded", "profit split"]),
    ("Account Access",         ["login", "password", "access", "locked", "2fa", "verification", "kyc"]),
    ("Technical Issues",       ["bug", "error", "crash", "not working", "issue", "problem", "broken", "slow"]),
    ("Evaluation & Challenge", ["evaluation", "challenge", "phase", "passed", "failed", "reset", "attempt"]),
    ("Compliance & Rules",     ["compliance", "rule", "violation", "breach", "terms", "policy", "restricted"]),
    ("General Enquiry",        []),   # catch-all
]


def _categorise(subject: str, preview: str) -> str:
    text = ((subject or "") + " " + (preview or "")).lower()
    for category, keywords in CATEGORIES:
        if any(k in text for k in keywords):
            return category
    return "General Enquiry"


def _fmt_seconds(seconds) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{round(part / total * 100)}%"


# ── Design system ───────────────────────────────────────────────────────────

D_BG       = "#0f1117"   # deep page background
D_SURFACE  = "#161b27"   # content surface
D_CARD     = "#1e2433"   # card background
D_CARD2    = "#252c3d"   # alternate card / table row
D_CARD3    = "#2a3245"   # hover / accent card
D_BORDER   = "#2e3650"   # subtle border
D_BORDER2  = "#3d4a66"   # stronger border
D_TEXT     = "#eef2f8"   # primary text
D_TEXT2    = "#c8d0e0"   # secondary text
D_MUTED    = "#7a859e"   # muted / labels
D_GREEN    = "#34d399"   # success
D_GREEN_BG = "#0d2e22"   # green card bg
D_RED      = "#f87171"   # danger
D_RED_BG   = "#2e1515"   # red card bg
D_YELLOW   = "#fbbf24"   # warning
D_YELLOW_BG= "#2e2410"   # yellow card bg
D_BLUE     = "#60a5fa"   # primary accent
D_BLUE_BG  = "#0f2040"   # blue card bg
D_PURPLE   = "#a78bfa"   # FIN / AI
D_PURPLE_BG= "#1e1535"   # purple card bg
D_TEAL     = "#2dd4bf"   # team / channel
D_TEAL_BG  = "#0a2825"   # teal card bg
D_ORANGE   = "#fb923c"   # escalation
D_ORANGE_BG= "#2e1a0a"   # orange card bg

GRAD_HEADER = "linear-gradient(135deg,#1a2744 0%,#0f1117 60%,#1a1535 100%)"


def _dark_section(title: str, subtitle: str = "") -> str:
    sub = f'<div style="font-size:11px;color:{D_MUTED};margin-top:3px">{subtitle}</div>' if subtitle else ""
    return (
        f'<div style="margin:32px 0 16px 0">'
        f'<div style="display:inline-block;background:{D_BLUE};width:3px;height:16px;'
        f'border-radius:2px;vertical-align:middle;margin-right:10px"></div>'
        f'<span style="font-size:11px;font-weight:700;color:{D_TEXT};text-transform:uppercase;'
        f'letter-spacing:2px;vertical-align:middle">{title}</span>'
        f'{sub}</div>'
    )


def _dbar(value: int, total: int, color: str = "#60a5fa", height: int = 4) -> str:
    pct = round(value / total * 100) if total else 0
    return (
        f'<div style="background:{D_CARD2};border-radius:99px;height:{height}px;width:100%">'
        f'<div style="background:{color};width:{pct}%;height:{height}px;border-radius:99px'
        f';box-shadow:0 0 6px {color}40"></div>'
        f'</div>'
    )


def _badge(text: str, color: str, bg: str) -> str:
    return f'<span style="display:inline-block;padding:2px 8px;background:{bg};color:{color};border-radius:99px;font-size:10px;font-weight:700;letter-spacing:0.5px">{text}</span>'


def build_report_html(summaries: List[Dict[str, Any]], report_date: datetime) -> str:
    """Build a dark-theme HTML analytics report from conversation summaries."""
    total = len(summaries)
    if total == 0:
        return f'<p style="color:{D_MUTED}">No conversations found.</p>'

    # ── Aggregate ─────────────────────────────────────────────────────────────
    _CHANNEL_LABELS = {
        "email": "Email",
        "chat": "Chat",
        "admin_initiated": "Admin-initiated",
        "unknown": "Other",
    }
    _CH_COLORS = {
        "email": D_BLUE,
        "chat": D_PURPLE,
        "admin_initiated": D_TEAL,
        "unknown": D_MUTED,
    }
    _CAT_COLORS = [D_GREEN, D_RED, "#f97316", D_PURPLE, D_BLUE, D_YELLOW, D_MUTED]

    by_state: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    by_agent_closed: Dict[str, int] = {}
    by_agent_reopened: Dict[str, int] = {}
    by_agent_parts: Dict[str, List[int]] = {}
    by_agent_first_resp: Dict[str, List[float]] = {}
    by_agent_median_resp: Dict[str, List[float]] = {}
    by_agent_handled: Dict[str, int] = {}  # total convs touched (teammate)
    by_agent_escalated: Dict[str, int] = {}
    by_agent_slow_frt: Dict[str, int] = {}
    by_agent_csat: Dict[str, List[int]] = {}
    by_category: Dict[str, int] = {}
    by_team: Dict[str, int] = {}
    by_tag: Dict[str, int] = {}
    first_resp_times: List[float] = []
    close_times: List[float] = []
    got_response = 0
    unhandled = 0
    reopened_total = 0
    escalated_total = 0
    escalated_conv_ids: List[str] = []
    multi_assigned_total = 0  # count_assignments > 1

    for s in summaries:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1
        ch = s["type"] if s["type"] else "unknown"
        by_channel[ch] = by_channel.get(ch, 0) + 1
        cat = _categorise(s.get("subject", ""), s.get("first_message_preview", ""))
        by_category[cat] = by_category.get(cat, 0) + 1

        # team breakdown
        tid = s.get("team_id", "")
        if tid:
            tname = TEAM_MAP.get(tid, f"Team {tid}")
            by_team[tname] = by_team.get(tname, 0) + 1

        # tags
        for tag in (s.get("tags") or []):
            if tag:
                by_tag[tag] = by_tag.get(tag, 0) + 1

        if s.get("time_to_first_response"):
            first_resp_times.append(s["time_to_first_response"])

        if s.get("time_to_close"):
            close_times.append(s["time_to_close"])
        if s.get("was_handled"):
            got_response += 1
        else:
            unhandled += 1
        reopens = s.get("reopened_count") or 0
        if reopens > 0:
            reopened_total += 1
        if (s.get("count_assignments") or 0) > 1:
            multi_assigned_total += 1
        if s.get("is_escalated"):
            escalated_total += 1
            if s.get("id"):
                escalated_conv_ids.append(str(s["id"]))

        parts = s.get("reply_count") or 0
        agent_id = s.get("closed_by_id", "")
        agent_name = ADMIN_MAP.get(agent_id, f"Agent {agent_id}") if agent_id else None
        is_fin_agent = any(k in (agent_name or "").lower() for k in FIN_KEYWORDS)
        if agent_name and not is_fin_agent:
            by_agent_closed[agent_name] = by_agent_closed.get(agent_name, 0) + 1
            by_agent_reopened[agent_name] = by_agent_reopened.get(agent_name, 0) + reopens
            by_agent_parts.setdefault(agent_name, []).append(parts)
            if s.get("time_to_first_response"):
                frt_val = s["time_to_first_response"]
                by_agent_first_resp.setdefault(agent_name, []).append(frt_val)
                if frt_val > 1800:
                    by_agent_slow_frt[agent_name] = by_agent_slow_frt.get(agent_name, 0) + 1
            if s.get("median_time_to_reply"):
                by_agent_median_resp.setdefault(agent_name, []).append(s["median_time_to_reply"])
            if s.get("is_escalated"):
                by_agent_escalated[agent_name] = by_agent_escalated.get(agent_name, 0) + 1

        # CSAT attributed to teammate who received rating
        if s.get("csat") and s.get("csat_teammate_id"):
            csat_agent = ADMIN_MAP.get(s["csat_teammate_id"], "")
            if csat_agent:
                by_agent_csat.setdefault(csat_agent, []).append(int(s["csat"]))

        # count all teammates who touched this conversation
        for tid2 in (s.get("teammate_ids") or []):
            tname2 = ADMIN_MAP.get(tid2, "")
            if tname2 and not any(k in tname2.lower() for k in FIN_KEYWORDS):
                by_agent_handled[tname2] = by_agent_handled.get(tname2, 0) + 1

    closed_count     = by_state.get("closed", 0)
    open_count       = by_state.get("open", 0)
    snoozed_count    = by_state.get("snoozed", 0)
    unassigned_count = sum(1 for s in summaries if not s.get("closed_by_id"))
    resolution_rate  = round(closed_count / total * 100) if total else 0
    avg_first_resp   = _trimmed_avg(first_resp_times)
    avg_ttc          = sum(close_times) / len(close_times) if close_times else None
    handled_pct      = round(got_response / total * 100) if total else 0
    unhandled_pct    = 100 - handled_pct

    # ── METRIC CARDS ──────────────────────────────────────────────────────────
    def _stat_card(value: str, label: str, accent: str, bg: str, sub: str = "") -> str:
        sub_html = f'<div style="font-size:11px;color:{D_MUTED};margin-top:5px">{sub}</div>' if sub else ""
        return (
            f'<td style="width:25%;padding:0 5px;vertical-align:top">'
            f'<div style="background:{bg};border:1px solid {accent}30;border-left:3px solid {accent};'
            f'border-radius:10px;padding:16px 14px 14px 14px">'
            f'<div style="font-size:10px;font-weight:600;color:{D_MUTED};text-transform:uppercase;'
            f'letter-spacing:1.2px;margin-bottom:10px">{label}</div>'
            f'<div style="font-size:30px;font-weight:800;color:{accent};line-height:1;letter-spacing:-1px">{value}</div>'
            f'{sub_html}'
            f'</div></td>'
        )

    row1 = (
        '<table style="width:100%;border-collapse:collapse;margin:0 -5px 8px -5px"><tr>'
        + _stat_card(str(total),          "Total",      D_BLUE,   D_BLUE_BG)
        + _stat_card(str(closed_count),   "Closed",     D_GREEN,  D_GREEN_BG,  f"{resolution_rate}% closure rate")
        + _stat_card(str(open_count),     "Still Open", D_RED,    D_RED_BG,    f"{snoozed_count} snoozed" if snoozed_count else "")
        + _stat_card(str(unassigned_count),"Unassigned", D_YELLOW, D_YELLOW_BG, f"{round(unassigned_count/total*100) if total else 0}% of total")
        + "</tr></table>"
    )
    row2 = (
        '<table style="width:100%;border-collapse:collapse;margin:0 -5px 28px -5px"><tr>'
        + _stat_card(_fmt_seconds(avg_first_resp), "Avg 1st Response", D_TEAL,   D_TEAL_BG)
        + _stat_card(_fmt_seconds(avg_ttc),        "Avg Time to Close",D_PURPLE, D_PURPLE_BG)
        + _stat_card(f"{handled_pct}%",            "Handled by Team",  D_GREEN,  D_GREEN_BG,  f"{got_response} of {total}")
        + _stat_card(f"{unhandled_pct}%",          "Unhandled",        D_RED,    D_RED_BG,    "no team response")
        + "</tr></table>"
    )

    # ── helper: a polished bar-table row ──────────────────────────────────────
    def _bar_row(label: str, cnt: int, tot: int, color: str, extra: str = "", dot: bool = True) -> str:
        pct_val = round(cnt / tot * 100) if tot else 0
        dot_html = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};margin-right:8px;vertical-align:middle;flex-shrink:0"></span>' if dot else ""
        return (
            f'<tr>'
            f'<td style="padding:11px 0 11px 0;border-bottom:1px solid {D_BORDER};white-space:nowrap;width:190px">'
            f'{dot_html}<span style="color:{D_TEXT2};font-size:12px">{label}</span></td>'
            f'<td style="padding:11px 14px 11px 14px;border-bottom:1px solid {D_BORDER};width:100%">{_dbar(cnt, tot, color)}</td>'
            f'<td style="padding:11px 0;border-bottom:1px solid {D_BORDER};text-align:right;white-space:nowrap">'
            f'<span style="font-weight:700;color:{D_TEXT};font-size:12px">{cnt}</span>'
            f'<span style="color:{D_MUTED};font-size:11px;margin-left:4px">{pct_val}%</span>'
            f'{(" " + extra) if extra else ""}</td>'
            f'</tr>'
        )

    # ── CONVERSATIONS BY CHANNEL ──────────────────────────────────────────────
    ch_rows = ""
    for ch, cnt in sorted(by_channel.items(), key=lambda x: -x[1]):
        color = _CH_COLORS.get(ch, D_MUTED)
        label = _CHANNEL_LABELS.get(ch, ch.replace("_", " ").title())
        ch_rows += _bar_row(label, cnt, total, color)
    channel_section = (
        _dark_section("CONVERSATIONS BY CHANNEL") +
        f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;padding:4px 16px 0 16px;margin-bottom:24px">'
        f'<table style="width:100%;border-collapse:collapse">{ch_rows}</table></div>'
    )

    # ── TOP ISSUES ────────────────────────────────────────────────────────────
    cat_rows = ""
    for i, (cat, cnt) in enumerate(sorted(by_category.items(), key=lambda x: -x[1])):
        color = _CAT_COLORS[i % len(_CAT_COLORS)]
        cat_rows += _bar_row(cat, cnt, total, color)
    category_section = (
        _dark_section("TOP ISSUES BY CATEGORY", f"{total} conversations sampled") +
        f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;padding:4px 16px 0 16px;margin-bottom:24px">'
        f'<table style="width:100%;border-collapse:collapse">{cat_rows}</table></div>'
    )

    # ── TEAM HANDLING SUMMARY ─────────────────────────────────────────────────
    team_section = (
        _dark_section("TEAM HANDLING OVERVIEW") +
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px"><tr>'
        f'<td style="width:50%;padding:0 6px 0 0;vertical-align:top">'
        f'<div style="background:{D_GREEN_BG};border:1px solid {D_GREEN}30;border-left:3px solid {D_GREEN};border-radius:10px;padding:18px 16px">'
        f'<div style="font-size:10px;font-weight:600;color:{D_MUTED};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">Handled by team</div>'
        f'<div style="font-size:36px;font-weight:800;color:{D_GREEN};line-height:1;letter-spacing:-1px">~{handled_pct}%</div>'
        f'<div style="font-size:11px;color:{D_MUTED};margin-top:6px">{got_response} conversations got a reply</div>'
        f'</div></td>'
        f'<td style="width:50%;padding:0 0 0 6px;vertical-align:top">'
        f'<div style="background:{D_RED_BG};border:1px solid {D_RED}30;border-left:3px solid {D_RED};border-radius:10px;padding:18px 16px">'
        f'<div style="font-size:10px;font-weight:600;color:{D_MUTED};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">Unhandled / bot-only</div>'
        f'<div style="font-size:36px;font-weight:800;color:{D_RED};line-height:1;letter-spacing:-1px">~{unhandled_pct}%</div>'
        f'<div style="font-size:11px;color:{D_MUTED};margin-top:6px">{unhandled} conversations with no team response</div>'
        f'</div></td>'
        f'</tr></table>'
    )

    # ── agent sort: fastest first-response time = rank 1 ─────────────────────
    def _agent_sort_key(a: str):
        frt = by_agent_first_resp.get(a, [])
        avg = sum(frt) / len(frt) if frt else float("inf")
        return (avg, -by_agent_closed.get(a, 0))

    # ── AGENT LEADERBOARD (quick close table) ─────────────────────────────────
    agent_section = ""
    if by_agent_closed:
        agent_rows_html = ""
        _lb_agents = sorted(by_agent_closed.keys(), key=_agent_sort_key)
        for rank, agent in enumerate(_lb_agents, 1):
            closed = by_agent_closed[agent]
            avg_p   = round(sum(by_agent_parts.get(agent, [0])) / max(len(by_agent_parts.get(agent, [1])), 1))
            reopens = by_agent_reopened.get(agent, 0)
            medal   = ["🥇","🥈","🥉"][rank-1] if rank <= 3 else f"#{rank}"
            row_bg  = D_CARD if rank % 2 == 1 else D_CARD2
            rp_color = D_RED if reopens > 0 else D_MUTED
            agent_rows_html += (
                f'<tr style="background:{row_bg}">'
                f'<td style="padding:11px 14px;border-bottom:1px solid {D_BORDER}">'
                f'<span style="font-size:13px;margin-right:8px">{medal}</span>'
                f'<span style="color:{D_TEXT};font-size:12px;font-weight:600">{agent}</span></td>'
                f'<td style="padding:11px 14px;border-bottom:1px solid {D_BORDER};text-align:center">'
                f'<span style="background:{D_GREEN_BG};color:{D_GREEN};padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid {D_GREEN}40">{closed}</span></td>'
                f'<td style="padding:11px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_MUTED};font-size:12px">{avg_p}</td>'
                f'<td style="padding:11px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{rp_color};font-size:12px;font-weight:600">{reopens}</td>'
                f'</tr>'
            )
        def _ath(label: str, align: str = "center") -> str:
            return f'<th style="padding:10px 14px;text-align:{align};font-size:9px;color:{D_MUTED};font-weight:700;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid {D_BORDER2}">{label}</th>'
        agent_section = (
            _dark_section("AGENT LEADERBOARD") +
            f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;overflow:hidden;margin-bottom:24px">'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr style="background:{D_CARD2}">'
            + _ath("Agent", "left") + _ath("Closed") + _ath("Avg Replies") + _ath("Reopens") +
            f'</tr></thead><tbody>{agent_rows_html}</tbody></table></div>'
        )

    # ── FIN / BOT HANDLING ────────────────────────────────────────────────
    fin_closed = 0
    human_closed = 0
    fin_conv_ids: List[str] = []      # IDs of FIN-closed conversations
    unhandled_conv_ids: List[str] = [] # IDs of completely unhandled convs
    for s in summaries:
        agent_id = s.get("closed_by_id", "")
        agent_name = ADMIN_MAP.get(agent_id, "").lower() if agent_id else ""
        is_fin = any(k in agent_name for k in FIN_KEYWORDS)
        if s["state"] == "closed":
            if is_fin:
                fin_closed += 1
                if s.get("id"):
                    fin_conv_ids.append(str(s["id"]))
            elif agent_id:
                human_closed += 1
        if not s.get("was_handled") and s.get("id"):
            unhandled_conv_ids.append(str(s["id"]))
    fin_pct   = round(fin_closed / closed_count * 100)   if closed_count else 0
    human_pct = round(human_closed / closed_count * 100) if closed_count else 0

    def _conv_pills(ids: List[str], color: str, bg: str, limit: int = 20) -> str:
        """Render conversation IDs as clickable pills."""
        if not ids:
            return f'<span style="font-size:12px;color:{D_MUTED}">None found</span>'
        shown = ids[:limit]
        st = f"display:inline-block;margin:3px 3px 3px 0;padding:3px 9px;background:{bg};color:{color};border:1px solid {color};border-radius:16px;font-size:11px;font-weight:600;text-decoration:none"
        pills = "".join(
            f'<a href="https://app.intercom.com/a/inbox/{INTERCOM_APP_ID}/inbox/shared/all/conversation/{cid}" style="{st}">#{cid}</a>'
            for cid in shown
        )
        extra = f'<span style="font-size:11px;color:{D_MUTED}"> +{len(ids)-limit} more</span>' if len(ids) > limit else ""
        return pills + extra

    def _mini_card(label: str, value: str, accent: str, bg: str, sub: str = "") -> str:
        sub_html = f'<div style="font-size:10px;color:{D_MUTED};margin-top:3px">{sub}</div>' if sub else ""
        return (
            f'<td style="width:33%;padding:0 5px;vertical-align:top">'
            f'<div style="background:{bg};border:1px solid {accent}25;border-left:3px solid {accent};border-radius:10px;padding:14px 12px">'
            f'<div style="font-size:9px;font-weight:600;color:{D_MUTED};text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">{label}</div>'
            f'<div style="font-size:26px;font-weight:800;color:{accent};letter-spacing:-0.5px;line-height:1">{value}</div>'
            f'{sub_html}</div></td>'
        )

    def _pill_box(title: str, ids: List[str], color: str, bg: str, extra_style: str = "") -> str:
        return (
            f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-left:3px solid {color};'
            f'border-radius:10px;padding:14px 16px;margin-top:10px{(";" + extra_style) if extra_style else ""}">' 
            f'<div style="font-size:9px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px">{title}</div>'
            f'{_conv_pills(ids, color, bg)}</div>'
        )

    fin_section = (
        _dark_section("FIN AI vs HUMAN HANDLING") +
        '<table style="width:100%;border-collapse:collapse;margin:0 -5px 0 -5px"><tr>'
        + _mini_card("FIN / Bot Closed",   str(fin_closed),     D_PURPLE, D_PURPLE_BG, f"{fin_pct}% of closed")
        + _mini_card("Human Team Closed",   str(human_closed),   D_GREEN,  D_GREEN_BG,  f"{human_pct}% of closed")
        + _mini_card("Reopened After Close", str(reopened_total), D_YELLOW, D_YELLOW_BG, "needed follow-up")
        + '</tr></table>'
        + _pill_box("FIN-closed conversations — review these", fin_conv_ids, D_PURPLE, D_PURPLE_BG)
        + _pill_box("Unhandled — no team response — action needed", unhandled_conv_ids, D_RED, D_RED_BG, "margin-bottom:24px")
    )

    # ── KEY QUESTIONS ──────────────────────────────────────────────────────
    top_cat    = max(by_category, key=by_category.get) if by_category else "N/A"
    top_ch     = _CHANNEL_LABELS.get(max(by_channel, key=by_channel.get), "N/A") if by_channel else "N/A"
    top_agent  = max(by_agent_closed, key=by_agent_closed.get) if by_agent_closed else None
    questions = [
        ("What drove the most volume?",
         f"<strong>{top_cat}</strong> was the top category ({by_category.get(top_cat,0)} conversations). Review if FAQs or automations could deflect these."),
        ("Are open conversations being followed up?",
         f"<strong>{open_count} conversations</strong> are still open. Ensure each has an owner before end of day."),
        ("Is FIN resolving or escalating correctly?",
         f"FIN closed <strong>{fin_closed}</strong> conversations ({fin_pct}%). Check if FIN-closed tickets match expected resolution topics."),
        ("Which channel needs more coverage?",
         f"<strong>{top_ch}</strong> was the busiest channel. Ensure staffing matches volume for this channel."),
    ]
    if top_agent:
        questions.append(("Who led the team today?",
            f"<strong>{top_agent}</strong> closed the most conversations ({by_agent_closed[top_agent]}). Recognise and replicate their approach."))
    if reopened_total > 0:
        questions.append(("Why are conversations being reopened?",
            f"<strong>{reopened_total} conversations</strong> were reopened. Review for incomplete resolutions or customer follow-ups."))

    q_items = "".join(
        f'<div style="border-bottom:1px solid {D_BORDER};padding:12px 0 12px 0">'
        f'<div style="font-size:10px;font-weight:700;color:{D_BLUE};text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">{q}</div>'
        f'<div style="color:{D_TEXT2};font-size:12px;line-height:1.6">{a}</div>'
        f'</div>'
        for q, a in questions
    )
    questions_section = (
        _dark_section("KEY QUESTIONS FOR TODAY") +
        f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;padding:4px 16px 12px 16px;margin-bottom:24px">{q_items}</div>'
    )

    # ── AGENT PERFORMANCE SCORECARD ───────────────────────────────────────────
    all_scorecard_agents = sorted(
        set(list(by_agent_closed.keys()) + list(by_agent_handled.keys())),
        key=_agent_sort_key
    )
    scorecard_rows = ""
    for rank, agent in enumerate(all_scorecard_agents, 1):
        closed       = by_agent_closed.get(agent, 0)
        handled      = by_agent_handled.get(agent, 0)
        parts_list   = by_agent_parts.get(agent, [])
        avg_parts    = round(sum(parts_list) / len(parts_list)) if parts_list else 0
        frt_list     = by_agent_first_resp.get(agent, [])
        avg_frt_all  = sum(frt_list) / len(frt_list) if frt_list else None
        avg_frt      = _trimmed_avg(frt_list)
        slow_frt     = by_agent_slow_frt.get(agent, 0)
        mtr_list     = by_agent_median_resp.get(agent, [])
        avg_mtr      = sum(mtr_list) / len(mtr_list) if mtr_list else None
        reopens      = by_agent_reopened.get(agent, 0)
        escalated    = by_agent_escalated.get(agent, 0)
        csat_list    = by_agent_csat.get(agent, [])
        avg_csat     = round(sum(csat_list) / len(csat_list), 1) if csat_list else None
        esc_rate     = round(escalated / closed * 100) if closed else 0
        row_bg       = D_CARD if rank % 2 == 1 else D_CARD2
        medals       = ["🥇","🥈","🥉"]
        rank_label   = medals[rank-1] if rank <= 3 else f"#{rank}"
        csat_str     = f"{avg_csat}/5" if avg_csat else "&mdash;"
        csat_color   = D_GREEN if avg_csat and avg_csat >= 4 else (D_YELLOW if avg_csat else D_MUTED)
        frt_color    = D_GREEN if avg_frt and avg_frt < 3600 else (D_YELLOW if avg_frt and avg_frt < 14400 else D_RED)
        frt_all_color = D_GREEN if avg_frt_all and avg_frt_all < 3600 else (D_YELLOW if avg_frt_all and avg_frt_all < 14400 else D_RED)
        scorecard_rows += (
            f'<tr style="background:{row_bg}">'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER}">'
            f'<span style="font-size:14px;margin-right:7px">{rank_label}</span>'
            f'<span style="color:{D_TEXT};font-size:12px;font-weight:700">{agent}</span></td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center">'
            f'<span style="background:{D_GREEN_BG};color:{D_GREEN};padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid {D_GREEN}30">{closed}</span></td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_TEXT2};font-size:12px">{handled}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{frt_all_color};font-size:12px">{_fmt_seconds(avg_frt_all)}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{frt_color};font-size:12px;font-weight:600">{_fmt_seconds(avg_frt)}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_MUTED};font-size:12px">{_fmt_seconds(avg_mtr)}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_MUTED};font-size:12px">{avg_parts}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_RED if reopens > 0 else D_MUTED};font-size:12px;font-weight:{"700" if reopens > 0 else "400"}">{reopens}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_ORANGE if esc_rate > 0 else D_MUTED};font-size:12px">{esc_rate}%</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{csat_color};font-size:12px;font-weight:600">{csat_str}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid {D_BORDER};text-align:center;color:{D_RED if slow_frt > 0 else D_MUTED};font-size:12px;font-weight:{"700" if slow_frt > 0 else "400"}">{slow_frt if slow_frt > 0 else "&mdash;"}</td>'
            f'</tr>'
        )

    def _th(label: str) -> str:
        return (f'<th style="padding:10px 14px;text-align:center;font-size:9px;color:{D_MUTED};'
                f'font-weight:700;text-transform:uppercase;letter-spacing:1px;'
                f'border-bottom:2px solid {D_BORDER2};white-space:nowrap">{label}</th>')

    scorecard_section = (
        _dark_section("AGENT PERFORMANCE SCORECARD", "ranked by total activity") +
        f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;overflow:hidden;margin-bottom:24px">'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<thead><tr style="background:{D_CARD2}">'
        f'<th style="padding:10px 14px;text-align:left;font-size:9px;color:{D_MUTED};font-weight:700;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid {D_BORDER2}">Agent</th>'
        + _th("Closed") + _th("Touched") + _th("1st Resp (All)") + _th("1st Resp (Outlier Removed)") + _th("Median Resp") + _th("Avg Msgs") + _th("Reopens") + _th("Esc Rate") + _th("CSAT") + _th(">30m 1st Resp") +
        f'</tr></thead><tbody>{scorecard_rows}</tbody></table></div>'
    ) if all_scorecard_agents else ""

    # ── TEAM BREAKDOWN ────────────────────────────────────────────────────────
    team_breakdown_section = ""
    if by_team:
        team_rows = ""
        for tname, cnt in sorted(by_team.items(), key=lambda x: -x[1]):
            team_rows += _bar_row(tname, cnt, total, D_TEAL)
        team_breakdown_section = (
            _dark_section("CONVERSATIONS BY TEAM") +
            f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;padding:4px 16px 0 16px;margin-bottom:24px">'
            f'<table style="width:100%;border-collapse:collapse">{team_rows}</table></div>'
        )

    # ── TAG ANALYTICS ─────────────────────────────────────────────────────────
    tag_section = ""
    if by_tag:
        tag_colors = [D_BLUE, D_PURPLE, D_YELLOW, D_TEAL, D_RED, D_GREEN, D_ORANGE, D_MUTED]
        tag_rows = ""
        for i, (tag, cnt) in enumerate(sorted(by_tag.items(), key=lambda x: -x[1])[:12]):
            color = tag_colors[i % len(tag_colors)]
            tag_rows += _bar_row(tag, cnt, total, color)
        tag_section = (
            _dark_section("TOP TAGS USED") +
            f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-radius:10px;padding:4px 16px 0 16px;margin-bottom:24px">'
            f'<table style="width:100%;border-collapse:collapse">{tag_rows}</table></div>'
        )

    # ── ESCALATION TRACKING ───────────────────────────────────────────────────
    esc_rate_overall = round(escalated_total / total * 100) if total else 0
    multi_assign_pct = round(multi_assigned_total / total * 100) if total else 0
    top_esc_agent    = max(by_agent_escalated, key=by_agent_escalated.get) if by_agent_escalated else None

    esc_agent_rows = ""
    for ag, cnt in sorted(by_agent_escalated.items(), key=lambda x: -x[1]):
        esc_agent_rows += _bar_row(ag, cnt, escalated_total or 1, D_ORANGE, dot=False)

    escalation_section = (
        _dark_section("ESCALATION & HANDOFF TRACKING") +
        '<table style="width:100%;border-collapse:collapse;margin:0 -5px 12px -5px"><tr>'
        + _mini_card("Escalated",       str(escalated_total),     D_ORANGE, D_ORANGE_BG, f"{esc_rate_overall}% of total")
        + _mini_card("Multi-assigned",  str(multi_assigned_total), D_PURPLE, D_PURPLE_BG, f"{multi_assign_pct}% of total")
        + _mini_card("Top Escalator",   top_esc_agent or "&mdash;", D_YELLOW, D_YELLOW_BG,
                     f"{by_agent_escalated.get(top_esc_agent,0)} escalated" if top_esc_agent else "")
        + '</tr></table>'
        + ((f'<div style="background:{D_CARD};border:1px solid {D_BORDER};border-left:3px solid {D_ORANGE};'
            f'border-radius:10px;padding:12px 16px;margin-bottom:8px">'
            f'<div style="font-size:9px;font-weight:700;color:{D_ORANGE};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">Escalations per agent</div>'
            f'<table style="width:100%;border-collapse:collapse">{esc_agent_rows}</table></div>'
        ) if esc_agent_rows else "")
        + _pill_box("Escalated conversation links", escalated_conv_ids[:20], D_ORANGE, D_ORANGE_BG, "margin-bottom:24px")
    )

    return (row1 + row2 + channel_section + category_section +
            team_breakdown_section + tag_section +
            team_section + fin_section +
            scorecard_section + agent_section +
            escalation_section + questions_section)


# ─── Email sender ─────────────────────────────────────────────────────────────

def send_report_email(subject: str, html_body: str) -> bool:
    """Send the HTML report via SMTP."""
    if not SENDER_ADDRESS or not SENDER_PASSWORD:
        logger.error("SMTP sender not configured (EMAIL_1_ADDRESS / EMAIL_1_PASSWORD missing)")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((SENDER_DISPLAY, SENDER_ADDRESS))
    msg["To"] = ", ".join(REPORT_RECIPIENTS)
    if REPORT_CC:
        msg["Cc"] = ", ".join(REPORT_CC)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    all_recipients = REPORT_RECIPIENTS + REPORT_CC + REPORT_BCC

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(SENDER_ADDRESS, SENDER_PASSWORD)
                server.sendmail(SENDER_ADDRESS, all_recipients, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SENDER_ADDRESS, SENDER_PASSWORD)
                server.sendmail(SENDER_ADDRESS, all_recipients, msg.as_string())

        logger.info(f"Report email sent to: {REPORT_RECIPIENTS}, cc: {REPORT_CC}, bcc: {REPORT_BCC}")
        return True
    except Exception as exc:
        logger.error(f"Failed to send report email: {exc}")
        return False


# ─── Main job ─────────────────────────────────────────────────────────────────

def run_daily_report():
    """Pull yesterday's data, generate the report, and email it."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    date_label = yesterday.strftime("%A, %d %B %Y")
    logger.info(f"Running daily report for {date_label}...")

    # 1. Refresh agent list
    fetch_admins()

    # 2. Fetch conversations
    raw_conversations = fetch_conversations_for_date(yesterday)
    if not raw_conversations:
        logger.warning("No conversations found for yesterday — sending empty report.")
        report_body = (
            f"<p style='color:#6b7280'>No customer support conversations were found on "
            f"<strong>{date_label}</strong>.</p>"
        )
    else:
        summaries = [summarise_conversation(c) for c in raw_conversations]
        report_body = build_report_html(summaries, yesterday)

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FTUK Support Report</title>
</head>
<body style="margin:0;padding:0;background:{D_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;color:{D_TEXT}">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{D_BG};padding:28px 16px 40px 16px">
<tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;width:100%">

  <!-- HEADER CARD -->
  <tr>
    <td style="padding:0 0 6px 0">
      <div style="background:linear-gradient(135deg,#1a2a4a 0%,#0f1117 55%,#1e1040 100%);border-radius:14px;padding:28px 28px 24px 28px;border:1px solid {D_BORDER2}">
        <!-- top row: brand + timestamp -->
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td>
            <div style="display:inline-block;background:{D_BLUE}18;border:1px solid {D_BLUE}40;border-radius:6px;padding:4px 10px;margin-bottom:14px">
              <span style="font-size:9px;font-weight:700;color:{D_BLUE};text-transform:uppercase;letter-spacing:2px">FTUK Intercom</span>
            </div>
          </td>
          <td align="right" style="vertical-align:top">
            <div style="font-size:10px;color:{D_MUTED};text-align:right">{generated_at}</div>
          </td>
        </tr></table>
        <!-- title -->
        <div style="font-size:26px;font-weight:800;color:{D_TEXT};letter-spacing:-0.5px;line-height:1.2;margin-bottom:6px">Customer Support<br>Daily Report</div>
        <div style="font-size:13px;color:{D_MUTED};margin-bottom:20px">{date_label}</div>
        <!-- accent bar -->
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="height:2px;background:{D_BLUE};border-radius:1px;width:40%"></td>
          <td style="height:2px;background:{D_PURPLE};border-radius:1px;width:30%"></td>
          <td style="height:2px;background:{D_TEAL};border-radius:1px;width:20%"></td>
          <td style="height:2px;background:{D_GREEN};border-radius:1px;width:10%"></td>
        </tr></table>
      </div>
    </td>
  </tr>

  <!-- CONTENT -->
  <tr>
    <td style="padding:12px 0 0 0">
      {report_body}
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:28px 0 0 0">
      <div style="border-top:1px solid {D_BORDER};padding-top:20px">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td>
            <span style="font-size:10px;color:{D_MUTED}">FTUK Intercom MCP &middot; {generated_at}</span>
          </td>
          <td align="right">
            <a href="https://app.intercom.com/a/inbox/{INTERCOM_APP_ID}" style="font-size:10px;color:{D_BLUE};text-decoration:none;font-weight:600">Open Intercom &rarr;</a>
          </td>
        </tr></table>
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    subject = f"FTUK Support Report — {yesterday.strftime('%d %b %Y')}"
    send_report_email(subject, full_html)
    logger.info("Daily report job complete.")


# ─── Entry point ──────────────────────────────────────────────────────────────

def validate_config():
    errors = []
    if not INTERCOM_API_TOKEN:
        errors.append("INTERCOM_API_TOKEN is not set")
    if not SENDER_ADDRESS or not SENDER_PASSWORD:
        errors.append("EMAIL_1_ADDRESS / EMAIL_1_PASSWORD not set — cannot send report emails")
    for e in errors:
        logger.warning(f"Config warning: {e}")


if __name__ == "__main__":
    validate_config()

    # Allow a --now flag to run immediately (useful for testing)
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        logger.info("Running report immediately (--now flag)...")
        run_daily_report()
        sys.exit(0)

    logger.info(f"Scheduler started. Report will run daily at {REPORT_SEND_TIME} UTC.")
    schedule.every().day.at(REPORT_SEND_TIME).do(run_daily_report)

    while True:
        schedule.run_pending()
        time.sleep(60)
