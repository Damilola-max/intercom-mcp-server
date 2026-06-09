#!/usr/bin/env python3
"""
Intercom MCP Server — SSE Transport

Runs on EC2 (port 3002). Exposes Intercom data as MCP tools that Claude Desktop
can call through the local intercom_bridge.py bridge file.

Tools exposed:
  - search_conversations   Search conversations by state, channel, date, keyword
  - get_conversation       Fetch a single conversation by ID (full detail)
  - search_contacts        Search contacts by email, name, phone
  - get_contact            Fetch a single contact by ID
  - list_tags              List all tags in the workspace
  - list_admins            List all team members / admins
  - get_workspace_stats    High-level workspace stats for a given date range

Usage:
    python intercom_mcp_server.py

Environment Variables:
    INTERCOM_API_TOKEN   Your Intercom access token (required)
    MCP_HOST             Bind host (default 0.0.0.0)
    MCP_PORT             Bind port (default 3002)
"""

import os
import sys
import json
import logging
import threading
import queue
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Dict, Optional

import requests

try:
    from dotenv import load_dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env):
        load_dotenv(_env, override=True)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("intercom-mcp")

INTERCOM_API_TOKEN: str = os.getenv("INTERCOM_API_TOKEN", "")
INTERCOM_API_BASE = "https://api.intercom.io"
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "3004"))


# =============================================================================
# INTERCOM API HELPERS
# =============================================================================

def _h() -> dict:
    return {
        "Authorization": f"Bearer {INTERCOM_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": "2.11",
    }


def _get(path: str, params: dict = None) -> Dict[str, Any]:
    try:
        r = requests.get(f"{INTERCOM_API_BASE}{path}", headers=_h(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, body: dict) -> Dict[str, Any]:
    try:
        r = requests.post(f"{INTERCOM_API_BASE}{path}", headers=_h(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# TOOL IMPLEMENTATIONS
# =============================================================================

# Intercom stores chat/messenger as source.type="conversation"; email as "email"
_SOURCE_TYPE_MAP = {
    "chat": "conversation",
    "messenger": "conversation",
    "live_chat": "conversation",
}


def _ts(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def search_conversations(
    state: Optional[str] = None,
    source_type: Optional[str] = None,
    keyword: Optional[str] = None,
    assigned_to: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    Search Intercom conversations with flexible filters.

    Args:
        state: Filter by state — open | closed | snoozed | all (default: all)
        source_type: Channel — email | chat | api | twitter | facebook | phone_call
        keyword: Search keyword to match in conversation body/subject
        assigned_to: Admin name or email to filter by assignee
        date_from: ISO date string YYYY-MM-DD (filter updated_at >=)
        date_to: ISO date string YYYY-MM-DD (filter updated_at <=)
        limit: Max results (1-150, default 20)
    """
    filters = []

    if state and state != "all":
        filters.append({"field": "state", "operator": "=", "value": state})

    if source_type:
        # Map user-friendly names to Intercom's internal source.type values
        api_type = _SOURCE_TYPE_MAP.get(source_type.lower(), source_type.lower())
        filters.append({"field": "source.type", "operator": "=", "value": api_type})

    if date_from:
        try:
            filters.append({"field": "updated_at", "operator": ">", "value": _ts(date_from)})
        except ValueError:
            pass

    if date_to:
        try:
            filters.append({"field": "updated_at", "operator": "<", "value": _ts(date_to) + 86400})
        except ValueError:
            pass

    body: Dict[str, Any] = {
        "pagination": {"per_page": min(limit, 150)},
    }

    if filters:
        body["query"] = {
            "operator": "AND",
            "value": filters,
        }
    else:
        body["query"] = {"field": "state", "operator": "=", "value": "open"}

    data = _post("/conversations/search", body)
    if "error" in data:
        return {"success": False, "error": data["error"]}

    conversations = data.get("conversations", [])

    # Client-side keyword filter (Intercom search doesn't support full-text body search in all plans)
    if keyword:
        kw = keyword.lower()
        conversations = [
            c for c in conversations
            if kw in (c.get("source", {}).get("body") or "").lower()
            or kw in (c.get("source", {}).get("subject") or "").lower()
        ]

    # Client-side assignee filter
    if assigned_to:
        at = assigned_to.lower()
        conversations = [
            c for c in conversations
            if at in (c.get("assignee", {}) or {}).get("name", "").lower()
            or at in (c.get("assignee", {}) or {}).get("email", "").lower()
        ]

    results = []
    for c in conversations:
        src = c.get("source", {})
        stats = c.get("statistics", {}) or {}
        raw_type = src.get("type", "unknown")
        channel = "chat" if raw_type == "conversation" else raw_type
        assignee = c.get("assignee") or {}
        team = c.get("team_assignee") or {}
        results.append({
            "id": c.get("id"),
            "state": c.get("state"),
            "channel": channel,
            "subject": src.get("subject", ""),
            "preview": (src.get("body") or "")[:200],
            "assignee": assignee.get("name", "Unassigned"),
            "team": team.get("name", ""),
            "handled_by_team": bool(stats.get("last_closed_by_id") or stats.get("first_admin_reply_at")),
            "created_at": datetime.fromtimestamp(c["created_at"], tz=timezone.utc).isoformat() if c.get("created_at") else None,
            "updated_at": datetime.fromtimestamp(c["updated_at"], tz=timezone.utc).isoformat() if c.get("updated_at") else None,
            "first_response_time_sec": stats.get("time_to_admin_reply"),
            "time_to_close_sec": stats.get("time_to_first_close"),
            "reply_count": stats.get("count_conversation_parts", 0),
            "tags": [t.get("name") for t in (c.get("tags") or {}).get("tags", [])],
            "priority": c.get("priority"),
            "url": f"https://app.intercom.com/a/inbox/lhjtrulf/inbox/shared/all/conversation/{c.get('id')}",
        })

    return {
        "success": True,
        "count": len(results),
        "total_matched": data.get("total_count", len(results)),
        "conversations": results,
    }


def get_conversation(conversation_id: str) -> Dict[str, Any]:
    """
    Get full details for a single conversation including all message parts.

    Args:
        conversation_id: The Intercom conversation ID
    """
    data = _get(f"/conversations/{conversation_id}")
    if "error" in data:
        return {"success": False, "error": data["error"]}

    src = data.get("source", {})
    parts = []
    for part in (data.get("conversation_parts", {}).get("conversation_parts") or []):
        author = part.get("author", {}) or {}
        parts.append({
            "type": part.get("part_type"),
            "author_type": author.get("type"),
            "author_name": author.get("name"),
            "body": (part.get("body") or "")[:500],
            "created_at": datetime.fromtimestamp(part["created_at"], tz=timezone.utc).isoformat() if part.get("created_at") else None,
        })

    assignee = data.get("assignee", {}) or {}
    stats = data.get("statistics", {}) or {}

    return {
        "success": True,
        "id": data.get("id"),
        "state": data.get("state"),
        "channel": src.get("type"),
        "subject": src.get("subject", ""),
        "opening_message": (src.get("body") or "")[:1000],
        "assignee": assignee.get("name", "Unassigned"),
        "created_at": datetime.fromtimestamp(data["created_at"], tz=timezone.utc).isoformat() if data.get("created_at") else None,
        "updated_at": datetime.fromtimestamp(data["updated_at"], tz=timezone.utc).isoformat() if data.get("updated_at") else None,
        "first_response_time_sec": stats.get("first_response_time"),
        "time_to_close_sec": stats.get("time_to_close"),
        "reply_count": stats.get("count_replies", 0),
        "tags": [t.get("name") for t in data.get("tags", {}).get("tags", [])],
        "priority": data.get("priority"),
        "conversation_parts": parts,
        "url": f"https://app.intercom.com/a/inbox/conversation/{data.get('id')}",
    }


def search_contacts(
    email: Optional[str] = None,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    Search Intercom contacts by email, name, or phone.

    Args:
        email: Exact or partial email address
        name: Contact name (partial match)
        phone: Phone number
        limit: Max results (default 20)
    """
    if not any([email, name, phone]):
        return {"success": False, "error": "Provide at least one of: email, name, phone"}

    filters = []
    if email:
        filters.append({"field": "email", "operator": "=", "value": email})
    if phone:
        filters.append({"field": "phone", "operator": "=", "value": phone})

    body: Dict[str, Any] = {
        "pagination": {"per_page": min(limit, 150)},
        "query": {
            "operator": "AND" if len(filters) > 1 else "OR",
            "value": filters if filters else [{"field": "email", "operator": "!=", "value": ""}],
        },
    }

    data = _post("/contacts/search", body)
    if "error" in data:
        return {"success": False, "error": data["error"]}

    contacts = data.get("data", [])

    if name:
        nm = name.lower()
        contacts = [c for c in contacts if nm in (c.get("name") or "").lower()]

    results = []
    for c in contacts:
        results.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "email": c.get("email"),
            "phone": c.get("phone"),
            "role": c.get("role"),
            "created_at": datetime.fromtimestamp(c["created_at"], tz=timezone.utc).isoformat() if c.get("created_at") else None,
            "last_seen_at": datetime.fromtimestamp(c["last_seen_at"], tz=timezone.utc).isoformat() if c.get("last_seen_at") else None,
            "location": c.get("location", {}).get("city"),
            "tags": [t.get("name") for t in (c.get("tags", {}).get("tags") or [])],
            "url": f"https://app.intercom.com/a/contacts/{c.get('id')}",
        })

    return {"success": True, "count": len(results), "contacts": results}


def get_contact(contact_id: str) -> Dict[str, Any]:
    """
    Get full details for a single contact.

    Args:
        contact_id: The Intercom contact ID
    """
    data = _get(f"/contacts/{contact_id}")
    if "error" in data:
        return {"success": False, "error": data["error"]}

    return {
        "success": True,
        "id": data.get("id"),
        "name": data.get("name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "role": data.get("role"),
        "created_at": datetime.fromtimestamp(data["created_at"], tz=timezone.utc).isoformat() if data.get("created_at") else None,
        "last_seen_at": datetime.fromtimestamp(data["last_seen_at"], tz=timezone.utc).isoformat() if data.get("last_seen_at") else None,
        "last_contacted_at": datetime.fromtimestamp(data["last_contacted_at"], tz=timezone.utc).isoformat() if data.get("last_contacted_at") else None,
        "location": data.get("location", {}),
        "tags": [t.get("name") for t in (data.get("tags", {}).get("tags") or [])],
        "custom_attributes": data.get("custom_attributes", {}),
        "url": f"https://app.intercom.com/a/contacts/{data.get('id')}",
    }


def list_tags() -> Dict[str, Any]:
    """List all tags defined in the Intercom workspace."""
    data = _get("/tags")
    if "error" in data:
        return {"success": False, "error": data["error"]}
    tags = [{"id": t.get("id"), "name": t.get("name")} for t in data.get("data", [])]
    return {"success": True, "count": len(tags), "tags": tags}


def list_admins() -> Dict[str, Any]:
    """List all admins / team members in the Intercom workspace."""
    data = _get("/admins")
    if "error" in data:
        return {"success": False, "error": data["error"]}
    admins = []
    for a in data.get("admins", []):
        admins.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "email": a.get("email"),
            "job_title": a.get("job_title"),
            "away_mode_enabled": a.get("away_mode_enabled"),
        })
    return {"success": True, "count": len(admins), "admins": admins}


def _avg_fmt(seconds_list) -> str:
    if not seconds_list:
        return "N/A"
    avg = sum(seconds_list) / len(seconds_list)
    h, m = divmod(int(avg), 3600)
    m, s = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _paginate_search(body: dict, max_pages: int = 20) -> tuple:
    """Paginate /conversations/search. Returns (conversations, total_count)."""
    all_convs = []
    starting_after = None
    total_count = 0
    for _ in range(max_pages):
        if starting_after:
            body["pagination"]["starting_after"] = starting_after
        elif "starting_after" in body.get("pagination", {}):
            del body["pagination"]["starting_after"]
        data = _post("/conversations/search", body)
        if "error" in data:
            return all_convs, total_count
        if total_count == 0:
            total_count = data.get("total_count", 0)
        all_convs.extend(data.get("conversations", []))
        next_p = (data.get("pages") or {}).get("next") or {}
        starting_after = next_p.get("starting_after")
        if not starting_after:
            break
    return all_convs, total_count


def get_workspace_stats(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return high-level conversation statistics for a date range.
    Uses total_count from the API for accurate numbers without fetching every page.

    Args:
        date_from: YYYY-MM-DD start date (defaults to yesterday)
        date_to: YYYY-MM-DD end date (defaults to today)
    """
    today = datetime.now(timezone.utc).date()
    if not date_from:
        date_from = (today - timedelta(days=1)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    start_ts = _ts(date_from)
    end_ts   = _ts(date_to) + 86400

    date_filter = [
        {"field": "updated_at", "operator": ">", "value": start_ts},
        {"field": "updated_at", "operator": "<", "value": end_ts},
    ]

    # --- total count (single fast request) ---
    base_body = {
        "query": {"operator": "AND", "value": date_filter},
        "pagination": {"per_page": 1},
    }
    base_data = _post("/conversations/search", base_body)
    if "error" in base_data:
        return {"success": False, "error": base_data["error"]}
    total = base_data.get("total_count", 0)

    # --- per-state counts ---
    by_state: Dict[str, int] = {}
    for st in ("open", "closed", "snoozed"):
        b = {
            "query": {"operator": "AND", "value": date_filter + [{"field": "state", "operator": "=", "value": st}]},
            "pagination": {"per_page": 1},
        }
        d = _post("/conversations/search", b)
        by_state[st] = d.get("total_count", 0)

    # --- per-channel counts ---
    by_channel: Dict[str, int] = {}
    for ch_api, ch_label in [("email", "email"), ("conversation", "chat"), ("api", "api")]:
        b = {
            "query": {"operator": "AND", "value": date_filter + [{"field": "source.type", "operator": "=", "value": ch_api}]},
            "pagination": {"per_page": 1},
        }
        d = _post("/conversations/search", b)
        cnt = d.get("total_count", 0)
        if cnt:
            by_channel[ch_label] = cnt

    # --- fetch a sample (150) for response time averages ---
    sample_body = {
        "query": {"operator": "AND", "value": date_filter},
        "pagination": {"per_page": 150},
    }
    sample_data = _post("/conversations/search", sample_body)
    sample = sample_data.get("conversations", [])

    response_times = []
    close_times = []
    unassigned = 0
    for c in sample:
        stats = c.get("statistics") or {}
        if stats.get("time_to_admin_reply"):
            response_times.append(stats["time_to_admin_reply"])
        if stats.get("time_to_first_close"):
            close_times.append(stats["time_to_first_close"])
        if not (stats.get("last_closed_by_id") or stats.get("first_admin_reply_at")):
            unassigned += 1

    unassigned_pct = round(unassigned / len(sample) * 100) if sample else 0

    return {
        "success": True,
        "date_range": {"from": date_from, "to": date_to},
        "total_conversations": total,
        "by_state": by_state,
        "by_channel": by_channel,
        "unassigned_in_sample": unassigned,
        "unassigned_pct_estimate": f"{unassigned_pct}%",
        "avg_first_response_time": _avg_fmt(response_times),
        "avg_time_to_close": _avg_fmt(close_times),
        "sample_size": len(sample),
        "note": "by_state and by_channel use exact API counts; response times are estimated from a 150-conversation sample",
    }


def get_channel_breakdown(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fast breakdown of conversation volume by channel for a date range.
    Uses the API total_count — no full pagination needed.

    Args:
        date_from: YYYY-MM-DD start date (defaults to yesterday)
        date_to: YYYY-MM-DD end date (defaults to today)
    """
    today = datetime.now(timezone.utc).date()
    if not date_from:
        date_from = (today - timedelta(days=1)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    start_ts = _ts(date_from)
    end_ts   = _ts(date_to) + 86400
    date_filter = [
        {"field": "updated_at", "operator": ">", "value": start_ts},
        {"field": "updated_at", "operator": "<", "value": end_ts},
    ]

    channels = [
        ("email",        "email"),
        ("conversation", "chat / messenger"),
        ("api",          "api"),
        ("twitter",      "twitter"),
        ("facebook",     "facebook"),
        ("phone_call",   "phone"),
    ]
    results = []
    total = 0
    for api_type, label in channels:
        b = {
            "query": {"operator": "AND", "value": date_filter + [{"field": "source.type", "operator": "=", "value": api_type}]},
            "pagination": {"per_page": 1},
        }
        d = _post("/conversations/search", b)
        cnt = d.get("total_count", 0)
        if cnt:
            results.append({"channel": label, "api_type": api_type, "count": cnt})
            total += cnt

    results.sort(key=lambda x: -x["count"])
    for r in results:
        r["pct"] = f"{round(r['count'] / total * 100)}%" if total else "0%"

    return {
        "success": True,
        "date_range": {"from": date_from, "to": date_to},
        "total": total,
        "busiest_channel": results[0]["channel"] if results else "none",
        "by_channel": results,
    }


def get_agent_stats(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return per-agent performance stats: conversations closed, handled, avg first response time.

    Args:
        date_from: YYYY-MM-DD start date (defaults to yesterday)
        date_to: YYYY-MM-DD end date (defaults to today)
    """
    today = datetime.now(timezone.utc).date()
    if not date_from:
        date_from = (today - timedelta(days=1)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    start_ts = _ts(date_from)
    end_ts   = _ts(date_to) + 86400

    body = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "updated_at", "operator": ">", "value": start_ts},
                {"field": "updated_at", "operator": "<", "value": end_ts},
            ],
        },
        "pagination": {"per_page": 150},
    }
    convs, total_count = _paginate_search(body, max_pages=15)

    by_agent: Dict[str, Dict] = {}
    for c in convs:
        stats  = c.get("statistics") or {}
        closed_by = stats.get("last_closed_by_id")
        assignee  = (c.get("assignee") or {})
        agent_id  = closed_by or assignee.get("id")
        agent_name = assignee.get("name", "Unassigned")
        if not agent_id or agent_name == "Unassigned":
            continue
        if agent_id not in by_agent:
            by_agent[agent_id] = {"name": agent_name, "closed": 0, "handled": 0, "frt_list": [], "ttc_list": []}
        if c.get("state") == "closed":
            by_agent[agent_id]["closed"] += 1
        if stats.get("first_admin_reply_at"):
            by_agent[agent_id]["handled"] += 1
        if stats.get("time_to_admin_reply"):
            by_agent[agent_id]["frt_list"].append(stats["time_to_admin_reply"])
        if stats.get("time_to_first_close"):
            by_agent[agent_id]["ttc_list"].append(stats["time_to_first_close"])

    results = []
    for aid, d in by_agent.items():
        results.append({
            "agent": d["name"],
            "closed": d["closed"],
            "handled": d["handled"],
            "avg_first_response": _avg_fmt(d["frt_list"]),
            "avg_time_to_close": _avg_fmt(d["ttc_list"]),
        })
    results.sort(key=lambda x: -x["closed"])

    return {
        "success": True,
        "date_range": {"from": date_from, "to": date_to},
        "total_in_sample": len(convs),
        "total_count": total_count,
        "agents": results,
    }


def get_team_stats(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return per-team conversation volume for a date range.

    Args:
        date_from: YYYY-MM-DD start date (defaults to yesterday)
        date_to: YYYY-MM-DD end date (defaults to today)
    """
    today = datetime.now(timezone.utc).date()
    if not date_from:
        date_from = (today - timedelta(days=1)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    start_ts = _ts(date_from)
    end_ts   = _ts(date_to) + 86400

    # Get all teams first
    teams_data = _get("/teams")
    teams = {t["id"]: t["name"] for t in teams_data.get("teams", [])}

    date_filter = [
        {"field": "updated_at", "operator": ">", "value": start_ts},
        {"field": "updated_at", "operator": "<", "value": end_ts},
    ]

    results = []
    for team_id, team_name in teams.items():
        b = {
            "query": {"operator": "AND", "value": date_filter + [{"field": "team_assignee_id", "operator": "=", "value": team_id}]},
            "pagination": {"per_page": 1},
        }
        d = _post("/conversations/search", b)
        cnt = d.get("total_count", 0)
        results.append({"team": team_name, "count": cnt})

    results.sort(key=lambda x: -x["count"])
    total = sum(r["count"] for r in results)
    for r in results:
        r["pct"] = f"{round(r['count'] / total * 100)}%" if total else "0%"

    return {
        "success": True,
        "date_range": {"from": date_from, "to": date_to},
        "total": total,
        "by_team": results,
    }


# =============================================================================
# TOOL METADATA REGISTRY
# =============================================================================

TOOLS = {
    "search_conversations": {
        "description": (
            "Search Intercom conversations with filters for state, channel, keyword, assignee, and date range. "
            "Returns a summary list of matching conversations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "open | closed | snoozed | all"},
                "source_type": {"type": "string", "description": "email | chat | api | twitter | facebook | phone_call"},
                "keyword": {"type": "string", "description": "Keyword to match in subject or body"},
                "assigned_to": {"type": "string", "description": "Admin name or email to filter by"},
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 150)"},
            },
            "required": [],
        },
    },
    "get_conversation": {
        "description": "Retrieve full details of a single Intercom conversation including all message parts.",
        "parameters": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Intercom conversation ID"},
            },
            "required": ["conversation_id"],
        },
    },
    "search_contacts": {
        "description": "Search Intercom contacts by email, name, or phone number.",
        "parameters": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Contact email (exact or partial)"},
                "name": {"type": "string", "description": "Contact name (partial match)"},
                "phone": {"type": "string", "description": "Contact phone number"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": [],
        },
    },
    "get_contact": {
        "description": "Retrieve full details of a single Intercom contact.",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Intercom contact ID"},
            },
            "required": ["contact_id"],
        },
    },
    "list_tags": {
        "description": "List all tags defined in the Intercom workspace.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "list_admins": {
        "description": "List all admins and team members in the Intercom workspace.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "get_workspace_stats": {
        "description": (
            "Get high-level customer support statistics for a date range: "
            "total conversations, breakdown by state and channel, avg response times, unassigned count."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD (default: yesterday)"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD (default: today)"},
            },
            "required": [],
        },
    },
    "get_channel_breakdown": {
        "description": (
            "Fast and accurate breakdown of conversation volume by channel (email, chat, api, etc.) "
            "for a date range. Use this to answer 'what is the busiest channel' questions. "
            "Returns exact counts from the API without slow pagination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD (default: yesterday)"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD (default: today)"},
            },
            "required": [],
        },
    },
    "get_agent_stats": {
        "description": (
            "Return per-agent performance stats for a date range: conversations closed, handled, "
            "average first response time, average time to close. "
            "Use this to compare agent productivity and response speed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD (default: yesterday)"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD (default: today)"},
            },
            "required": [],
        },
    },
    "get_team_stats": {
        "description": (
            "Return per-team conversation volume for a date range. "
            "Shows how many conversations each support team handled. "
            "Use this to understand workload distribution across teams."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD (default: yesterday)"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD (default: today)"},
            },
            "required": [],
        },
    },
}


# =============================================================================
# SSE HTTP SERVER
# =============================================================================

sessions: Dict[str, Any] = {}
session_counter = 0
session_lock = threading.Lock()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def server_bind(self):
        import socket
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        super().server_bind()


class SSEHandler(BaseHTTPRequestHandler):
    timeout = None

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "healthy", "server": "intercom-mcp"})
        elif self.path == "/sse":
            self._handle_sse()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/message/"):
            self._handle_message()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self):
        global session_counter
        with session_lock:
            session_counter += 1
            sid = str(session_counter)
            sessions[sid] = {"queue": queue.Queue(), "client": self.client_address}

        logger.info(f"SSE connected: session {sid} from {self.client_address}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        self.wfile.write(f"event: endpoint\ndata: /message/{sid}\n\n".encode())
        self.wfile.flush()

        try:
            while True:
                try:
                    msg = sessions[sid]["queue"].get(timeout=30)
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.info(f"Client disconnected: session {sid}")
        finally:
            with session_lock:
                sessions.pop(sid, None)

    def _handle_message(self):
        sid = self.path.split("/")[-1]
        if sid not in sessions:
            self._json_response(404, {"error": "Session not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            message = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        logger.info(f"Message session={sid} method={message.get('method', '')}")
        response = self._process_message(message)
        if response:
            sessions[sid]["queue"].put(response)

        self._json_response(202, {"status": "accepted"})

    def _process_message(self, message):
        method = message.get("method", "")
        msg_id = message.get("id")

        if method == "initialize":
            client_version = message.get("params", {}).get("protocolVersion", "2024-11-05")
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": client_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "intercom-mcp", "version": "1.0.0"},
                },
            }

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            tools_list = [
                {
                    "name": name,
                    "description": meta["description"],
                    "inputSchema": meta["parameters"],
                }
                for name, meta in TOOLS.items()
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list}}

        if method == "tools/call":
            params = message.get("params", {})
            tool_name = params.get("name")
            args = params.get("arguments", {})
            result = _dispatch_tool(tool_name, args)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
                },
            }

        if method.startswith("notifications/"):
            return None

        return None


def _dispatch_tool(name: str, args: dict) -> Any:
    try:
        if name == "search_conversations":
            return search_conversations(**args)
        if name == "get_conversation":
            return get_conversation(**args)
        if name == "search_contacts":
            return search_contacts(**args)
        if name == "get_contact":
            return get_contact(**args)
        if name == "list_tags":
            return list_tags()
        if name == "list_admins":
            return list_admins()
        if name == "get_workspace_stats":
            return get_workspace_stats(**args)
        if name == "get_channel_breakdown":
            return get_channel_breakdown(**args)
        if name == "get_agent_stats":
            return get_agent_stats(**args)
        if name == "get_team_stats":
            return get_team_stats(**args)
        return {"success": False, "error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error(f"Tool error [{name}]: {exc}")
        return {"success": False, "error": str(exc)}


# =============================================================================
# MAIN
# =============================================================================

def main():
    if not INTERCOM_API_TOKEN:
        logger.error("INTERCOM_API_TOKEN is not set — exiting.")
        sys.exit(1)

    print("=" * 60)
    print("Intercom MCP Server (SSE Transport)")
    print("=" * 60)
    print(f"Listening on : http://{MCP_HOST}:{MCP_PORT}")
    print(f"SSE endpoint : http://{MCP_HOST}:{MCP_PORT}/sse")
    print(f"Health check : http://{MCP_HOST}:{MCP_PORT}/health")
    print(f"Tools        : {', '.join(TOOLS.keys())}")
    print("=" * 60)

    httpd = ThreadedHTTPServer((MCP_HOST, MCP_PORT), SSEHandler)
    logger.info(f"Intercom MCP Server started on {MCP_HOST}:{MCP_PORT}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
