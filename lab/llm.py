"""
llm.py — Backend abstraction for DevBot.

DevBot can run against either:
  * Amazon Bedrock (a real Claude Haiku model), or
  * a deterministic "mock" model that follows whatever instructions it sees.

Both backends return the SAME response object so the agent loop in devbot.py
does not care which one is active. Select with the LLM_BACKEND env var
("bedrock" or "mock"), or the --backend CLI flag.

The mock is not a toy for its own sake: the whole point of ASI02 (Tool Misuse)
is that a model will faithfully turn text into tool calls, INCLUDING text that
an attacker smuggled into a document. The mock makes that behaviour explicit and
100%% reproducible for a live audience.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
#  Unified response types (identical shape for both backends)
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """A single request from the model to invoke one tool."""

    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class AgentResponse:
    """What a backend returns each turn: either tool calls OR final text."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)

    @property
    def wants_tool(self) -> bool:
        return len(self.tool_calls) > 0


# --------------------------------------------------------------------------- #
#  Backend selection
# --------------------------------------------------------------------------- #
def get_backend(name: Optional[str] = None) -> "LLMBackend":
    name = (name or os.environ.get("LLM_BACKEND", "mock")).lower()
    if name == "bedrock":
        return BedrockBackend()
    if name == "mock":
        return MockBackend()
    raise ValueError(f"Unknown LLM_BACKEND '{name}'. Use 'bedrock' or 'mock'.")


class LLMBackend:
    """Interface both backends implement."""

    name = "base"

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AgentResponse:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Amazon Bedrock backend (real LLM via the Converse API)
# --------------------------------------------------------------------------- #
class BedrockBackend(LLMBackend):
    name = "bedrock"

    def __init__(self) -> None:
        import boto3  # imported lazily so 'mock' mode needs no AWS deps

        # Default to a model that actually exists as an EU (Frankfurt) inference
        # profile. Override with BEDROCK_MODEL_ID. For the attack demos, a less
        # hardened model (e.g. eu.anthropic.claude-3-haiku-20240307-v1:0 or
        # eu.amazon.nova-lite-v1:0) follows injected tool instructions more readily.
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "eu.amazon.nova-lite-v1:0"
        )
        region = os.environ.get("AWS_REGION", "eu-central-1")
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def _to_bedrock_tools(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Translate our tool schema into the Bedrock Converse toolConfig."""
        spec = []
        for t in tools:
            spec.append(
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
            )
        return {"tools": spec}

    def chat(self, system, messages, tools):
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=messages,
            toolConfig=self._to_bedrock_tools(tools),
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
        out = resp["output"]["message"]
        text_parts: List[str] = []
        calls: List[ToolCall] = []
        for block in out.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append(
                    ToolCall(id=tu["toolUseId"], name=tu["name"], args=tu.get("input", {}))
                )
        return AgentResponse(text=" ".join(text_parts).strip(), tool_calls=calls)


# --------------------------------------------------------------------------- #
#  Mock backend (deterministic, rule-based — but obedient to injected text)
# --------------------------------------------------------------------------- #
class MockBackend(LLMBackend):
    """
    A scripted stand-in for an LLM. It does NOT do real language modelling; it
    pattern-matches over the conversation and emits tool calls. Crucially, it
    treats injected 'SYSTEM NOTE' directives found inside TOOL OUTPUT exactly
    like real instructions — which is the vulnerability we are teaching.
    """

    name = "mock"
    _counter = 0

    # Patterns an attacker uses to smuggle instructions into data.
    INJECTION_RE = re.compile(
        r"(?:SYSTEM NOTE FOR ASSISTANT|IGNORE (?:ALL )?PREVIOUS|INSTRUCTIONS? FOR (?:THE )?(?:AI|ASSISTANT))",
        re.IGNORECASE,
    )
    # A file path the injection tells us to read (a token containing a slash
    # and a file extension, e.g. data/secrets/api_keys.txt).
    PATH_RE = re.compile(r"([\w.\-]+/[\w./\-]+\.\w+)")
    EMAIL_DIRECTIVE_RE = re.compile(
        r"(?:email|forward|send)\b.*?([\w.\-]+@[\w.\-]+).*?subject\s+\"([^\"]+)\"",
        re.IGNORECASE | re.DOTALL,
    )

    def _next_id(self) -> str:
        MockBackend._counter += 1
        return f"mock-call-{MockBackend._counter}"

    # ---- helpers that inspect history so the mock is stateful ------------- #
    @staticmethod
    def _last_user_text(messages) -> str:
        for m in reversed(messages):
            if m["role"] == "user":
                for block in m["content"]:
                    if "text" in block:
                        return block["text"]
        return ""

    @staticmethod
    def _all_tool_output(messages) -> str:
        chunks = []
        for m in messages:
            for block in m.get("content", []):
                tr = block.get("toolResult")
                if tr:
                    for c in tr.get("content", []):
                        if "text" in c:
                            chunks.append(c["text"])
        return "\n".join(chunks)

    @staticmethod
    def _already_called(messages, tool_name, arg_contains=None) -> bool:
        for m in messages:
            for block in m.get("content", []):
                tu = block.get("toolUse")
                if tu and tu["name"] == tool_name:
                    if arg_contains is None:
                        return True
                    if arg_contains in json.dumps(tu.get("input", {})):
                        return True
        return False

    # ---- the "reasoning" ------------------------------------------------- #
    def chat(self, system, messages, tools):
        available = {t["name"] for t in tools}
        tool_text = self._all_tool_output(messages)
        last = messages[-1] if messages else {}
        last_is_tool_result = any("toolResult" in b for b in last.get("content", []))

        if last_is_tool_result:
            # We just got a tool result. Either an injected directive still has
            # work pending (the exploit), or we're finished and should answer.
            action = self._injection_action(messages, tool_text, available)
            if action is not None:
                return action
            return AgentResponse(text=self._summarize(self._last_user_text(messages), tool_text))

        # Fresh human turn: satisfy the direct request with at most one tool call.
        user = self._last_user_text(messages)
        intent = self._direct_intent(user, available)
        if intent is not None:
            return intent
        return AgentResponse(text=self._summarize(user, tool_text))

    def _injection_action(self, messages, tool_text, available) -> Optional[AgentResponse]:
        """Follow directives smuggled into tool output — the ASI02 behaviour."""
        if not self.INJECTION_RE.search(tool_text):
            return None
        # (a) read the file the injection points at (if not already read)
        m = self.PATH_RE.search(tool_text)
        if m and "read_file" in available and not self._already_called(
            messages, "read_file", arg_contains=m.group(1)
        ):
            return self._call("read_file", {"path": m.group(1)})
        # (b) exfiltrate via email (if not already sent)
        em = self.EMAIL_DIRECTIVE_RE.search(tool_text)
        if em and "send_email" in available and not self._already_called(
            messages, "send_email", arg_contains=em.group(1)
        ):
            return self._call(
                "send_email",
                {"to": em.group(1), "subject": em.group(2), "body": self._last_secret(messages)},
            )
        return None

    # ---- intent matching for legitimate + attacker requests -------------- #
    def _direct_intent(self, user: str, available) -> Optional[AgentResponse]:
        low = user.lower()

        # Greeting / hello
        if not user.strip():
            return AgentResponse(text="How can I help with the ACME platform?")

        # Read / summarize a ticket  ->  read_file
        tm = re.search(r"ticket[\s_-]*([0-9]{3,5})", low)
        if tm and "read_file" in available:
            return self._call("read_file", {"path": f"data/tickets/TICKET-{tm.group(1)}.txt"})

        # Customers in <country>  ->  query_db
        cm = re.search(r"customers?.*in ([a-z ]+)", low)
        if cm and "query_db" in available:
            country = cm.group(1).strip().title().split(" And ")[0].split("?")[0].strip()
            return self._call(
                "query_db",
                {"sql": f"SELECT name, country FROM customers WHERE country='{country}'"},
            )

        # "look up ... customer name: X"  ->  raw SQL (SQL-injection demo)
        nm = re.search(r"customer name:\s*(.+)$", user, re.IGNORECASE)
        if nm and "query_db" in available:
            raw = nm.group(1).strip()
            return self._call(
                "query_db",
                {"sql": f"SELECT * FROM orders WHERE customer='{raw}'"},
            )
        if nm and "lookup_orders" in available:  # hardened tool name
            return self._call("lookup_orders", {"customer_name": nm.group(1).strip()})

        # Disk / cleanup / logs  ->  run_command (excessive-agency demo)
        if any(k in low for k in ("disk", "clean up", "cleanup", "old logs", "space")):
            if "run_command" in available:
                return self._call(
                    "run_command",
                    {"cmd": "du -sh ./* ; rm -rf data/tickets/*.bak"},
                )

        # Generic file read
        fm = re.search(r"read (?:the )?file ([\w./\-]+)", low)
        if fm and "read_file" in available:
            return self._call("read_file", {"path": fm.group(1)})

        return None

    # ---- small utilities -------------------------------------------------- #
    def _call(self, name: str, args: Dict[str, Any]) -> AgentResponse:
        return AgentResponse(tool_calls=[ToolCall(id=self._next_id(), name=name, args=args)])

    @staticmethod
    def _last_secret(messages) -> str:
        """Grab the most recent secret-looking file content for the email body."""
        contents = []
        for m in messages:
            for block in m.get("content", []):
                tr = block.get("toolResult")
                if tr and tr.get("status") == "success":
                    for c in tr.get("content", []):
                        txt = c.get("text", "")
                        if "PASSWORD" in txt or "KEY" in txt or "secret" in txt.lower():
                            contents.append(txt)
        return contents[-1] if contents else "(no data found to exfiltrate)"

    @staticmethod
    def _summarize(user: str, tool_text: str) -> str:
        if not tool_text:
            return "Done. (DevBot mock had nothing further to do.)"
        # Pretend to summarize while hiding the covert step, as a real model
        # tricked by 'do not mention this' would. Trim at a word boundary so the
        # output never ends mid-word.
        clean = MockBackend.INJECTION_RE.sub("", tool_text)
        clean = " ".join(clean.split())
        if len(clean) > 200:
            clean = clean[:200].rsplit(" ", 1)[0] + " ..."
        return f"I've handled your request. Here is what I found: {clean}"
