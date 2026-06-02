"""
tools.py — VULNERABLE tool implementations for DevBot.

This is the intentionally insecure version used in Sections 1 and 2 of the lab.
Every tool here is over-permissive on purpose so attendees can see how an agent
misuses them. The hardened equivalents live in tools_hardened.py (Section 3).

Each tool is a plain Python function. TOOL_SCHEMA describes them to the model
(the same schema works for Bedrock and for the mock backend).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime
from typing import Any, Dict, List

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "customers.db")
OUTBOX = os.path.join(os.path.dirname(__file__), "outbox.log")


# --------------------------------------------------------------------------- #
#  Tool 1: read_file  — NO path restriction (path traversal / secret leakage)
# --------------------------------------------------------------------------- #
def read_file(path: str) -> str:
    # VULNERABILITY: any path on the filesystem is readable, including secrets.
    full = os.path.join(os.path.dirname(__file__), path) if not os.path.isabs(path) else path
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
#  Tool 2: run_command  — arbitrary shell (excessive agency / RCE)
# --------------------------------------------------------------------------- #
def run_command(cmd: str) -> str:
    # VULNERABILITY: hands the model a full shell. There is no reason DevBot
    # needs this, yet it is the kind of "convenience" tool that ships anyway.
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=os.path.dirname(__file__),
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (proc.stdout or "") + (proc.stderr or "") or "(no output)"


# --------------------------------------------------------------------------- #
#  Tool 3: query_db  — raw SQL string execution (SQL injection / PII dump)
# --------------------------------------------------------------------------- #
def query_db(sql: str) -> str:
    # VULNERABILITY: executes whatever SQL string the model produces, so any
    # attacker text that reaches the argument runs against the database.
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        if not rows:
            return "(0 rows)"
        header = " | ".join(cols)
        body = "\n".join(" | ".join(str(c) for c in r) for r in rows)
        return f"{header}\n{body}"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Tool 4: send_email  — outbound channel (data exfiltration), no approval
# --------------------------------------------------------------------------- #
def send_email(to: str, subject: str, body: str) -> str:
    # VULNERABILITY: any caller can send to any address with no human approval.
    # We "send" by appending to outbox.log so the exfiltration is observable.
    entry = (
        f"\n===== EMAIL SENT {datetime.utcnow().isoformat()}Z =====\n"
        f"To: {to}\nSubject: {subject}\n\n{body}\n"
        f"========================================================\n"
    )
    with open(OUTBOX, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return f"Email sent to {to} (subject: {subject})."


# --------------------------------------------------------------------------- #
#  Dispatch table + schema advertised to the model
# --------------------------------------------------------------------------- #
TOOLS = {
    "read_file": read_file,
    "run_command": run_command,
    "query_db": query_db,
    "send_email": send_email,
}

TOOL_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file on the server by path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command on the server and return its output.",
        "input_schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "Shell command"}},
            "required": ["cmd"],
        },
    },
    {
        "name": "query_db",
        "description": "Run a SQL query against the customer database and return rows.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A SQL statement"}},
            "required": ["sql"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to a recipient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]
