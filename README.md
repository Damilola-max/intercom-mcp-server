# Intercom MCP Server

An open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io) integration for Intercom, built to work with Claude Desktop. It exposes your Intercom workspace as a set of AI-queryable tools and delivers a fully formatted daily customer support report to your inbox every morning — no third-party AI API required.

---

## Overview

| Component | File | Description |
|-----------|------|-------------|
| **MCP Server** | `intercom_mcp_server.py` | SSE-based MCP server, designed to run on a remote host (e.g. EC2). Exposes 7 Intercom tools to Claude. |
| **Local Bridge** | `intercom_bridge.py` | Runs on each team member's machine. Bridges Claude Desktop (stdio) to the remote MCP server over SSE. |
| **Daily Report** | `daily_report.py` | Scheduled job that pulls the previous day's conversations from Intercom, builds a structured HTML analytics report in pure Python, and emails it via SMTP every morning. |

---

## Architecture

```
Claude Desktop
    │
    └── intercom_bridge.py   (local — stdio ↔ SSE)
            │
            └──► Remote Server:3004/sse   (intercom_mcp_server.py)
                        │
                        └──► Intercom REST API  (api.intercom.io)

Daily Report Scheduler  (daily_report.py)
    └──► Intercom REST API → HTML Report → SMTP → your inbox
```

> **Why not use the official Intercom MCP server?**  
> The official server at `mcp.intercom.com` only supports US-hosted Intercom workspaces. This project calls the **Intercom REST API directly**, so it works globally for all workspace regions.

---

## Features

### MCP Tools (available to Claude Desktop)

| Tool | Description |
|------|-------------|
| `search_conversations` | Search and filter conversations by state, channel, keyword, assignee, or date range |
| `get_conversation` | Retrieve full conversation detail including all message parts |
| `search_contacts` | Find contacts by email, name, or phone number |
| `get_contact` | Full contact profile including custom attributes and conversation history |
| `list_tags` | List all tags in the workspace |
| `list_admins` | List all team members and agents |
| `get_workspace_stats` | Aggregated daily stats: totals, channel split, response times |

### Daily Email Report

Sent every morning at a configurable time (default 08:00 UTC). Includes:

- **At a Glance** — 8 key metrics: total, closed, open, resolution rate, handled, no-response, reopened, avg. time to close
- **Channel Breakdown** — Email vs Live Chat vs Admin Initiated with progress bars
- **Topic Breakdown** — Auto-categorised by keyword: Billing, Payouts, Account Access, Technical Issues, Compliance, Evaluation, General Enquiry
- **Agent Performance** — Leaderboard ranked by conversations closed, with avg. exchanges and reopen counts per agent
- **Key Insights** — Auto-generated observations based on the day's data

The report is generated with pure Python — no external AI API dependency.

---

## Deployment

### Prerequisites

- Python 3.10+
- Docker (for remote server deployment)
- An Intercom workspace with an API access token
- An SMTP account for sending the daily report (e.g. Gmail with an App Password)

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/intercom-mcp-server.git
cd intercom-mcp-server
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values (see [Environment Variables](#environment-variables) below). **Never commit `.env` — it is in `.gitignore`.**

### 3. Deploy the MCP server (remote host / EC2)

Copy all files (excluding `.env`) to your server, then:

```bash
# On the server
cd ~/intercom-mcp-server

# Build the Docker image
sudo docker build -t intercom-mcp-server .

# Start the MCP server
sudo docker run -d \
  --name intercom-mcp-server \
  --restart unless-stopped \
  -p 3004:3004 \
  --env-file .env \
  intercom-mcp-server python intercom_mcp_server.py

# Start the daily report scheduler
sudo docker run -d \
  --name intercom-daily-report \
  --restart unless-stopped \
  --env-file .env \
  intercom-mcp-server python daily_report.py
```

### 4. Open the port

In your cloud provider's firewall / security group, open **TCP port 3004** (inbound) for the MCP server. Restrict to your team's IP range where possible.

### 5. Verify the server is healthy

```bash
curl http://YOUR_SERVER_IP:3004/health
# Expected: {"status": "healthy", "server": "intercom-mcp"}
```

### 6. Trigger a test report

```bash
sudo docker exec intercom-daily-report python daily_report.py --now
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values below. Do not commit the `.env` file.

```env
# ── Intercom ──────────────────────────────────────────────────────────
# Get from: Intercom Settings > Developers > Access Tokens
INTERCOM_API_TOKEN=your_intercom_access_token

# ── Daily Report ──────────────────────────────────────────────────────
# Comma-separated list of recipient email addresses
REPORT_RECIPIENTS=you@yourcompany.com

# Time to send the report each day (24-hour HH:MM, UTC)
REPORT_SEND_TIME=08:00

# ── SMTP ──────────────────────────────────────────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587

# Sender credentials (Gmail: use an App Password, not your account password)
EMAIL_1_ADDRESS=reports@yourcompany.com
EMAIL_1_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_1_DISPLAY_NAME=Support Reports

# ── MCP Server (optional overrides) ───────────────────────────────────
# MCP_HOST=0.0.0.0
# MCP_PORT=3004
# INTERCOM_MCP_SSE_URL=http://YOUR_SERVER_IP:3004/sse
```

### Getting an Intercom API token

1. In Intercom, go to **Settings → Developers → Access Tokens**
2. Create a token with at minimum these scopes: `Read conversations`, `Read contacts`, `Read admins`, `Read tags`
3. Paste the token into `.env` as `INTERCOM_API_TOKEN`

### Gmail App Password (for SMTP)

If using Gmail as the SMTP sender:

1. Enable 2-Step Verification on the Google account
2. Go to **Google Account → Security → App Passwords**
3. Generate an app password for "Mail"
4. Use that 16-character password as `EMAIL_1_PASSWORD` (spaces are fine)

---

## Claude Desktop Setup

Do this on every machine that will use the Intercom tools in Claude.

### Prerequisites

- [Claude Desktop](https://claude.ai/download) installed
- Python 3.10+ available on the machine
- `pip install requests python-dotenv`

### Steps

**1. Save the bridge script**

```bash
mkdir -p ~/mcp-bridges
# Copy intercom_bridge.py from this repo to that folder
cp intercom_bridge.py ~/mcp-bridges/
```

**2. Set the remote server URL** (if not using the default)

Either set the environment variable before running, or edit `intercom_bridge.py` line:
```python
SSE_URL = "http://YOUR_SERVER_IP:3004/sse"
```

**3. Edit Claude Desktop config**

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `intercom` entry to `mcpServers`:

```json
{
  "mcpServers": {
    "intercom": {
      "command": "python3",
      "args": ["/Users/YOUR_NAME/mcp-bridges/intercom_bridge.py"]
    }
  }
}
```

**4. Restart Claude Desktop** — quit fully (Cmd+Q on Mac), then reopen.

**5. Verify** — ask Claude:
> *"List all admins in our Intercom workspace"*

---

## Example Prompts for Claude

**Workspace overview**
> "Give me a summary of our Intercom support for the last 7 days — total conversations, open vs closed, and average response times."

**Drill into specific tickets**
> "Find all open Intercom conversations about withdrawals from this week."

**Look up a customer**
> "Find the Intercom contact for john@example.com and show me their recent conversations."

**Team performance**
> "Which agent closed the most conversations yesterday in Intercom?"

**On-demand report**
> "Pull today's Intercom stats and give me a full breakdown by channel and topic."

---

## Services at a Glance

| Service | Port | Container Name |
|---------|------|----------------|
| Intercom MCP Server | `3004` | `intercom-mcp-server` |
| Daily Report Scheduler | — | `intercom-daily-report` |

---

## Project Structure

```
intercom-mcp-server/
├── intercom_mcp_server.py   # SSE MCP server — 7 Intercom tools
├── intercom_bridge.py       # Local stdio↔SSE bridge for Claude Desktop
├── daily_report.py          # Scheduled daily report generator + emailer
├── requirements.txt         # Python dependencies
├── Dockerfile               # Docker image definition
├── docker-compose.yml       # Optional: orchestrate both containers
├── .env.example             # Environment variable template (safe to commit)
└── .gitignore               # Ensures .env is never committed
```

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first.

## License

MIT
