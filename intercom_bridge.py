#!/usr/bin/env python3
"""
Intercom MCP Bridge

Local bridge for Claude Desktop. Translates MCP stdio ↔ SSE traffic
to the Intercom MCP Server running on EC2 (port 3002).

Usage (Claude Desktop config):
    {
      "mcpServers": {
        "intercom": {
          "command": "python3",
          "args": ["/path/to/intercom_bridge.py"]
        }
      }
    }

Environment Variables (set in a .env next to this file, or system env):
    INTERCOM_MCP_SSE_URL   EC2 SSE URL (default: http://13.222.176.24:3002/sse)
"""

import os
import sys
import json
import logging
import threading
import queue
import requests
from typing import Optional

try:
    from dotenv import load_dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env):
        load_dotenv(_env, override=True)
except ImportError:
    pass

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("intercom-bridge")

SSE_URL: str = os.getenv("INTERCOM_MCP_SSE_URL", "http://13.222.176.24:3004/sse")
_message_endpoint: Optional[str] = None
_response_queue: queue.Queue = queue.Queue()
_session = requests.Session()


def _sse_listener():
    """Background thread: connect to SSE, parse endpoint event, forward data lines."""
    global _message_endpoint
    try:
        with _session.get(SSE_URL, stream=True, timeout=None) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines(chunk_size=1):
                if isinstance(raw, bytes):
                    raw = raw.decode()
                if not raw:
                    continue
                if raw.startswith("event: endpoint"):
                    continue
                if raw.startswith("data:"):
                    value = raw[5:].strip()
                    if value.startswith("/message/"):
                        base = SSE_URL.rsplit("/sse", 1)[0]
                        _message_endpoint = base + value
                        logger.warning(f"Message endpoint: {_message_endpoint}")
                    else:
                        try:
                            _response_queue.put(json.loads(value))
                        except json.JSONDecodeError:
                            pass
    except Exception as exc:
        logger.error(f"SSE listener error: {exc}")
        sys.exit(1)


def _send(payload: dict) -> Optional[dict]:
    """POST a JSON-RPC payload to the message endpoint, return response from queue."""
    global _message_endpoint
    # Wait up to 15 s for endpoint to be established
    for _ in range(150):
        if _message_endpoint:
            break
        import time; time.sleep(0.1)

    if not _message_endpoint:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32603, "message": "SSE endpoint not ready"}}

    try:
        _session.post(_message_endpoint, json=payload, timeout=60)
        return _response_queue.get(timeout=60)
    except queue.Empty:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32603, "message": "Response timeout"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32603, "message": str(exc)}}


def run_bridge():
    t = threading.Thread(target=_sse_listener, daemon=True)
    t.start()

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"}
            }) + "\n")
            sys.stdout.flush()
            continue

        response = _send(payload)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_bridge()
