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
import json
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
REPORT_SEND_TIME: str = os.getenv("REPORT_SEND_TIME", "08:00")

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SENDER_ADDRESS: str = os.getenv("EMAIL_1_ADDRESS", "")
SENDER_PASSWORD: str = os.getenv("EMAIL_1_PASSWORD", "").replace(" ", "")
SENDER_DISPLAY: str = os.getenv("EMAIL_1_DISPLAY_NAME", "FTUK Support")

INTERCOM_API_BASE = "https://api.intercom.io"

# Known agents — fallback map (updated by fetch_admins at runtime)
ADMIN_MAP: Dict[str, str] = {
    "8770830": "Nick Quinn",
    "8770831": "Bot / Assistant",
    "8889691": "Dami",
    "9022355": "ReZa",
    "9023402": "Umar",
    "9030249": "Tom",
    "10383433": "Bradley",
    "10384010": "Nanda",
    "10390175": "Navin",
}

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
    stats = conv.get("statistics", {})
    assignee = conv.get("assignee", {}) or {}

    # Normalise Intercom source types
    raw_type = source.get("type", "unknown")
    _TYPE_MAP = {
        "conversation": "chat",
        "email": "email",
        "admin_initiated": "admin_initiated",
    }
    channel = _TYPE_MAP.get(raw_type, raw_type)

    # assignee: null in list/search endpoints — derive from last_closed_by_id presence
    assignee_name = (assignee.get("name") or "") if assignee else ""

    return {
        "id": conv.get("id"),
        "type": channel,
        "subject": source.get("subject", ""),
        "state": conv.get("state", "unknown"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "assignee_name": assignee_name if assignee_name else "Unassigned",
        "was_handled": bool(stats.get("last_closed_by_id") or stats.get("first_admin_reply_at")),
        "closed_by_id": str(stats.get("last_closed_by_id") or ""),
        "first_response_time": stats.get("time_to_admin_reply"),
        "time_to_close": stats.get("time_to_first_close"),
        "reopened_count": stats.get("count_reopens", 0),
        "reply_count": stats.get("count_conversation_parts", 0),
        "tags": [t.get("name") for t in conv.get("tags", {}).get("tags", [])],
        "priority": conv.get("priority", "not_priority"),
        "read": conv.get("read", False),
        "first_message_preview": (source.get("body") or "")[:300],
    }


# ─── Analytics report builder (pure Python) ───────────────────────────────────

# Keywords used to auto-categorise conversations by subject / preview
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


def _stat_row(label: str, value: str, highlight: bool = False) -> str:
    bg = "#fef9c3" if highlight else "#ffffff"
    return (
        f'<tr style="background:{bg}">'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;color:#6b7280;width:55%">{label}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-weight:600">{value}</td>'
        f"</tr>"
    )


def _card(value: str, label: str, color: str = "#1f2937", bg: str = "#f8fafc") -> str:
    return (
        f'<td style="width:25%;padding:0 8px 16px 8px;vertical-align:top">'
        f'<div style="background:{bg};border:1px solid #e2e8f0;border-left:4px solid {color};'
        f'border-radius:8px;padding:18px 16px">'
        f'<div style="font-size:26px;font-weight:800;color:{color};line-height:1">{value}</div>'
        f'<div style="font-size:11px;color:#64748b;margin-top:6px;font-weight:500;letter-spacing:0.3px">{label}</div>'
        f'</div></td>'
    )


def _bar(value: int, total: int, color: str = "#3b82f6") -> str:
    pct = round(value / total * 100) if total else 0
    return (
        f'<div style="background:#f1f5f9;border-radius:999px;height:6px;width:100%;margin-top:6px">'
        f'<div style="background:{color};width:{pct}%;height:6px;border-radius:999px"></div>'
        f'</div>'
    )


def _section_heading(title: str, icon_svg: str) -> str:
    return (
        f'<table width="100%" style="margin:28px 0 14px 0;border-collapse:collapse"><tr>'
        f'<td style="width:28px;vertical-align:middle;padding-right:10px">{icon_svg}</td>'
        f'<td style="vertical-align:middle">'
        f'<span style="font-size:13px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:0.8px">{title}</span>'
        f'</td>'
        f'<td style="border-bottom:2px solid #e2e8f0"></td>'
        f'</tr></table>'
    )


ICON_OVERVIEW  = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>'
ICON_CHANNEL   = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
ICON_TOPIC     = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>'
ICON_AGENTS    = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
ICON_INSIGHTS  = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'


def build_report_html(summaries: List[Dict[str, Any]], report_date: datetime) -> str:
    """Build a fully structured HTML analytics report from conversation summaries."""
    total = len(summaries)
    date_str = report_date.strftime("%A, %d %B %Y")

    if total == 0:
        return f"<p>No conversations were found for <strong>{date_str}</strong>.</p>"

    # ── Aggregate data ───────────────────────────────────────────────────────
    by_state: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    by_agent_closed: Dict[str, int] = {}   # agent name -> conversations closed
    by_agent_reopened: Dict[str, int] = {} # agent name -> reopens on their closed convs
    by_agent_parts: Dict[str, List[int]] = {}  # agent name -> list of part counts
    by_category: Dict[str, int] = {}
    close_times: List[float] = []
    unhandled = 0
    reopened_total = 0
    high_reply: List[Dict] = []

    _CHANNEL_LABELS = {
        "email": "Email",
        "chat": "Live Chat",
        "admin_initiated": "Admin Initiated",
        "unknown": "Other",
    }

    for s in summaries:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1
        ch = s["type"] if s["type"] else "unknown"
        by_channel[ch] = by_channel.get(ch, 0) + 1

        if not s.get("was_handled"):
            unhandled += 1

        cat = _categorise(s.get("subject", ""), s.get("first_message_preview", ""))
        by_category[cat] = by_category.get(cat, 0) + 1

        if s.get("time_to_close"):
            close_times.append(s["time_to_close"])

        reopens = s.get("reopened_count") or 0
        if reopens > 0:
            reopened_total += 1

        parts = s.get("reply_count") or 0
        if parts >= 10:
            high_reply.append(s)

        # Per-agent breakdown using last_closed_by_id
        agent_id = s.get("closed_by_id", "")
        agent_name = ADMIN_MAP.get(agent_id, f"Agent {agent_id}") if agent_id else None
        if agent_name and agent_name != "Bot / Assistant":
            by_agent_closed[agent_name] = by_agent_closed.get(agent_name, 0) + 1
            by_agent_reopened[agent_name] = by_agent_reopened.get(agent_name, 0) + reopens
            if agent_name not in by_agent_parts:
                by_agent_parts[agent_name] = []
            by_agent_parts[agent_name].append(parts)

    avg_ttc = sum(close_times) / len(close_times) if close_times else None
    closed_count = by_state.get("closed", 0)
    open_count   = by_state.get("open", 0)
    handled      = total - unhandled
    resolution_rate = round(closed_count / total * 100) if total else 0

    # ── STAT CARDS ───────────────────────────────────────────────────────────
    stat_cards = (
        _section_heading("At a Glance", ICON_OVERVIEW) +
        f'<table style="width:100%;border-collapse:collapse;margin:0 -8px 4px -8px"><tr>'
        f'{_card(str(total), "Total Conversations", "#3b82f6", "#eff6ff")}'
        f'{_card(str(closed_count), "Closed", "#10b981", "#f0fdf4")}'
        f'{_card(str(open_count), "Still Open", "#ef4444" if open_count > 5 else "#f59e0b", "#fff7ed")}'
        f'{_card(f"{resolution_rate}%", "Resolution Rate", "#10b981" if resolution_rate >= 90 else "#f59e0b", "#f0fdf4" if resolution_rate >= 90 else "#fffbeb")}'
        f'</tr><tr>'
        f'{_card(str(handled), "Handled by Team", "#6366f1", "#eef2ff")}'
        f'{_card(str(unhandled), "No Admin Response", "#ef4444" if unhandled > 0 else "#10b981", "#fef2f2" if unhandled > 0 else "#f0fdf4")}'
        f'{_card(str(reopened_total), "Reopened", "#f59e0b" if reopened_total > 0 else "#10b981", "#fffbeb" if reopened_total > 0 else "#f0fdf4")}'
        f'{_card(_fmt_seconds(avg_ttc), "Avg. Time to Close", "#8b5cf6", "#f5f3ff")}'
        f'</tr></table>'
    )

    # ── CHANNEL BREAKDOWN ────────────────────────────────────────────────────
    ch_colors = {"email": "#3b82f6", "chat": "#8b5cf6", "admin_initiated": "#10b981", "unknown": "#94a3b8"}
    channel_rows = "".join(
        f'<tr>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;color:#1e293b;font-weight:500;font-size:13px">'
        f'  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{ch_colors.get(ch,"#94a3b8")};margin-right:8px;vertical-align:middle"></span>'
        f'  {_CHANNEL_LABELS.get(ch, ch.replace("_"," ").title())}</td>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;font-weight:700;width:50px;text-align:right;color:#0f172a">{cnt}</td>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;width:180px">'
        f'  {_bar(cnt, total, ch_colors.get(ch,"#94a3b8"))}'
        f'  <span style="font-size:11px;color:#94a3b8;font-weight:500">{_pct(cnt,total)}</span></td>'
        f'</tr>'
        for ch, cnt in sorted(by_channel.items(), key=lambda x: -x[1])
    )
    channel_section = (
        _section_heading("Channel Breakdown", ICON_CHANNEL) +
        f'<table style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:8px;overflow:hidden">'
        f'{channel_rows}</table>'
    )

    # ── TOPIC CATEGORIES ─────────────────────────────────────────────────────
    cat_colors = ["#3b82f6","#8b5cf6","#ec4899","#f59e0b","#10b981","#ef4444","#64748b"]
    cat_rows = "".join(
        f'<tr>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;color:#1e293b;font-size:13px">{cat}</td>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;font-weight:700;width:50px;text-align:right;color:#0f172a">{cnt}</td>'
        f'<td style="padding:11px 12px;border-bottom:1px solid #f1f5f9;width:180px">'
        f'  {_bar(cnt, total, cat_colors[i % len(cat_colors)])}'
        f'  <span style="font-size:11px;color:#94a3b8;font-weight:500">{_pct(cnt,total)}</span></td>'
        f'</tr>'
        for i, (cat, cnt) in enumerate(sorted(by_category.items(), key=lambda x: -x[1]))
    )
    category_section = (
        _section_heading("Topic Breakdown", ICON_TOPIC) +
        f'<table style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:8px;overflow:hidden">'
        f'{cat_rows}</table>'
    )

    # ── AGENT LEADERBOARD ────────────────────────────────────────────────────
    RANK_COLORS = ["#f59e0b", "#94a3b8", "#b45309"]
    agent_rows_html = ""
    if by_agent_closed:
        sorted_agents = sorted(by_agent_closed.items(), key=lambda x: -x[1])
        for rank, (agent, closed) in enumerate(sorted_agents, 1):
            avg_parts = round(sum(by_agent_parts.get(agent, [0])) / max(len(by_agent_parts.get(agent, [1])), 1))
            reopens = by_agent_reopened.get(agent, 0)
            rank_color = RANK_COLORS[rank-1] if rank <= 3 else "#64748b"
            rank_label = ["1st","2nd","3rd"][rank-1] if rank <= 3 else f"#{rank}"
            row_bg = "#f0fdf4" if rank == 1 else ("#fafafa" if rank % 2 == 0 else "#ffffff")
            agent_rows_html += (
                f'<tr style="background:{row_bg}">'
                f'<td style="padding:13px 16px;border-bottom:1px solid #f1f5f9">'
                f'  <span style="display:inline-block;background:{rank_color};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;margin-right:8px;letter-spacing:0.5px">{rank_label}</span>'
                f'  <span style="font-weight:600;color:#0f172a;font-size:13px">{agent}</span></td>'
                f'<td style="padding:13px 16px;border-bottom:1px solid #f1f5f9;text-align:center">'
                f'  <span style="background:#dcfce7;color:#15803d;padding:4px 12px;border-radius:20px;font-weight:700;font-size:13px">{closed}</span></td>'
                f'<td style="padding:13px 16px;border-bottom:1px solid #f1f5f9;text-align:center;color:#64748b;font-size:13px">{avg_parts}</td>'
                f'<td style="padding:13px 16px;border-bottom:1px solid #f1f5f9;text-align:center">'
                f'  <span style="color:{"#dc2626" if reopens > 0 else "#15803d"};font-weight:600;font-size:13px">{reopens}</span></td>'
                f'</tr>'
            )
        agent_section = (
            _section_heading("Agent Performance", ICON_AGENTS) +
            f'<table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden">'
            f'<thead><tr style="background:#f1f5f9">'
            f'<th style="padding:10px 16px;text-align:left;font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Agent</th>'
            f'<th style="padding:10px 16px;text-align:center;font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Closed</th>'
            f'<th style="padding:10px 16px;text-align:center;font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Avg. Exchanges</th>'
            f'<th style="padding:10px 16px;text-align:center;font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Reopens</th>'
            f'</tr></thead>'
            f'<tbody>{agent_rows_html}</tbody></table>'
        )
    else:
        agent_section = ""

    # ── KEY OBSERVATIONS ─────────────────────────────────────────────────────
    observations: List[str] = []
    top_cat = max(by_category, key=by_category.get) if by_category else None
    if top_cat:
        observations.append(
            f"<strong>{top_cat}</strong> was the top issue "
            f"({by_category[top_cat]} conversations, {_pct(by_category[top_cat], total)})."
        )
    top_channel = max(by_channel, key=by_channel.get) if by_channel else None
    if top_channel:
        observations.append(
            f"<strong>{_CHANNEL_LABELS.get(top_channel, top_channel.title())}</strong> was the main channel "
            f"({by_channel[top_channel]}, {_pct(by_channel[top_channel], total)})."
        )
    if unhandled > 0:
        observations.append(
            f"<span style='color:#dc2626'><strong>{unhandled} conversation{'s' if unhandled!=1 else ''} received no admin response</strong></span> — needs immediate attention."
        )
    if reopened_total > 0:
        observations.append(
            f"{reopened_total} conversation{'s were' if reopened_total!=1 else ' was'} reopened — first resolutions may need review."
        )
    if high_reply:
        observations.append(
            f"{len(high_reply)} complex case{'s' if len(high_reply)!=1 else ''} required 10+ message parts."
        )
    if resolution_rate == 100:
        observations.append("<span style='color:#16a34a'><strong>100% resolution rate</strong> — outstanding team performance today!</span>")
    elif open_count > total * 0.3:
        observations.append(
            f"<span style='color:#d97706'><strong>{_pct(open_count,total)} of conversations still open</strong> — consider follow-up capacity.</span>"
        )
    if by_agent_closed:
        top_agent = max(by_agent_closed, key=by_agent_closed.get)
        observations.append(f"Top performer today: <strong>{top_agent}</strong> closed {by_agent_closed[top_agent]} conversations.")

    obs_html = "".join(
        f'<tr><td style="padding:11px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#1e293b">'
        f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#3b82f6;margin-right:10px;vertical-align:middle"></span>'
        f'{o}</td></tr>'
        for o in observations
    )
    observations_section = (
        _section_heading("Key Insights", ICON_INSIGHTS) +
        f'<table style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:8px;overflow:hidden">{obs_html}</table>'
    )

    return stat_cards + channel_section + category_section + agent_section + observations_section


# ─── Email sender ─────────────────────────────────────────────────────────────

def send_report_email(subject: str, html_body: str) -> bool:
    """Send the HTML report via SMTP."""
    if not SENDER_ADDRESS or not SENDER_PASSWORD:
        logger.error("SMTP sender not configured (EMAIL_1_ADDRESS / EMAIL_1_PASSWORD missing)")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((SENDER_DISPLAY, SENDER_ADDRESS))
    msg["To"] = ", ".join(REPORT_RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(SENDER_ADDRESS, SENDER_PASSWORD)
                server.sendmail(SENDER_ADDRESS, REPORT_RECIPIENTS, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SENDER_ADDRESS, SENDER_PASSWORD)
                server.sendmail(SENDER_ADDRESS, REPORT_RECIPIENTS, msg.as_string())

        logger.info(f"Report email sent to: {REPORT_RECIPIENTS}")
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
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:40px 20px">
<tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;width:100%">

  <!-- TOP BAR -->
  <tr>
    <td style="background:#0f172a;border-radius:12px 12px 0 0;padding:0">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:24px 32px 0 32px">
            <div style="font-size:10px;color:#3b82f6;font-weight:700;text-transform:uppercase;letter-spacing:2px">FTUK Funded Trading</div>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 32px 0 32px">
            <div style="font-size:24px;font-weight:800;color:#ffffff;letter-spacing:-0.5px">Customer Support Report</div>
          </td>
        </tr>
        <tr>
          <td style="padding:6px 32px 0 32px">
            <div style="font-size:13px;color:#64748b">{date_label}</div>
          </td>
        </tr>
        <!-- accent stripe -->
        <tr>
          <td style="padding:20px 0 0 0">
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
              <td style="height:4px;background:#3b82f6;width:33.3%"></td>
              <td style="height:4px;background:#8b5cf6;width:33.3%"></td>
              <td style="height:4px;background:#10b981;width:33.4%"></td>
            </tr></table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- CONTENT -->
  <tr>
    <td style="background:#ffffff;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;padding:32px 32px 16px 32px">
      {report_body}
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="background:#f8fafc;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;padding:16px 32px">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="font-size:11px;color:#94a3b8">Generated {generated_at}</td>
        <td align="right" style="font-size:11px">
          <a href="https://app.intercom.com" style="color:#3b82f6;text-decoration:none;font-weight:500">Open Intercom &rarr;</a>
        </td>
      </tr></table>
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
