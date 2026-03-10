"""Gmail MCP client for fetching application-related emails.

Uses the @gongrzhe/server-gmail-autoauth-mcp package over stdio transport
via the Python `mcp` SDK.

The MCP server returns plain text (not JSON), so we parse structured fields
from the text output.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# MCP credentials directory
GMAIL_MCP_DIR = Path.home() / ".gmail-mcp"
OAUTH_KEYS_PATH = GMAIL_MCP_DIR / "gcp-oauth.keys.json"

# The To: header shows the alias address. Gmail's `to:` operator
# matches on the header, so we filter by the alias directly.
RECIPIENT = "alex@elninja.com"


def check_gmail_setup() -> tuple[bool, str]:
    """Verify Gmail MCP prerequisites are in place.

    Returns:
        (ok, message) — ok is True if setup is valid.
    """
    creds_path = GMAIL_MCP_DIR / "credentials.json"
    if not OAUTH_KEYS_PATH.exists():
        return False, (
            f"Gmail OAuth credentials not found at {OAUTH_KEYS_PATH}\n\n"
            "Setup steps:\n"
            "  1. Go to https://console.cloud.google.com/\n"
            "  2. Create project → Enable Gmail API\n"
            "  3. Create OAuth 2.0 credentials (Desktop app type)\n"
            "  4. Download JSON → save as ~/.gmail-mcp/gcp-oauth.keys.json\n"
            "  5. Run: python3 scripts/gmail_oauth.py"
        )
    if not creds_path.exists():
        return False, (
            f"OAuth token not found at {creds_path}\n"
            "Run: python3 scripts/gmail_oauth.py"
        )
    return True, "Gmail MCP credentials found."


async def _create_mcp_client():
    """Create an MCP client connected to the Gmail server via stdio."""
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        env={
            **os.environ,
            "GMAIL_MCP_DIR": str(GMAIL_MCP_DIR),
        },
    )

    return stdio_client(server_params)


async def _call_tool_raw(session, tool_name: str, arguments: dict) -> str:
    """Call an MCP tool and return the raw text response."""
    log.debug("MCP call: %s(%s)", tool_name, json.dumps(arguments)[:200])
    result = await asyncio.wait_for(
        session.call_tool(tool_name, arguments=arguments),
        timeout=120,
    )
    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    text = "\n".join(parts)
    log.debug("MCP response (%s): %s", tool_name, text[:300])
    return text


# Import ClientSession at module level for verify_connection
try:
    from mcp import ClientSession
except ImportError:
    ClientSession = None


async def verify_connection() -> bool:
    """Test Gmail MCP connectivity by listing tools.

    Returns True if the server responds successfully.
    """
    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                tools = await session.list_tools()
                tool_names = [t.name for t in tools.tools]
                log.info("Gmail MCP tools: %s", tool_names)
                return bool(tool_names)
    except Exception as e:
        log.error("Gmail MCP connection failed: %s", e)
        return False


def _parse_search_results(text: str) -> list[dict]:
    """Parse the plain-text search_emails response into a list of email dicts.

    Format:
        ID: 19c9c2fc461f421f
        Subject: ...
        From: Name <email@example.com>
        Date: Thu, 26 Feb 2026 23:01:41 +0000 (UTC)

        ID: 19c9c1ecbca9ceb5
        ...
    """
    if text.startswith("Error:"):
        log.warning("Gmail search error: %s", text[:200])
        return []

    emails = []
    # Split on blank lines followed by "ID:"
    blocks = re.split(r'\n\n(?=ID:\s)', text.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        email = {}
        for line in block.split("\n"):
            if line.startswith("ID: "):
                email["id"] = line[4:].strip()
            elif line.startswith("Subject: "):
                email["subject"] = line[9:].strip()
            elif line.startswith("From: "):
                email["from"] = line[6:].strip()
            elif line.startswith("Date: "):
                email["date"] = line[6:].strip()
            elif line.startswith("To: "):
                email["to"] = line[4:].strip()
            elif line.startswith("Snippet: "):
                email["snippet"] = line[9:].strip()

        if email.get("id"):
            emails.append(email)

    return emails


def _parse_read_result(text: str, msg_id: str) -> dict:
    """Parse the plain-text read_email response into an email dict.

    Format:
        Thread ID: 19c9c1ecbca9ceb5
        Subject: ...
        From: Name <email@example.com>
        To: ...
        Date: ...

        Body text here...
    """
    if text.startswith("Error:"):
        log.warning("Gmail read error for %s: %s", msg_id, text[:200])
        return {"id": msg_id}

    email = {"id": msg_id}
    lines = text.split("\n")
    header_done = False
    body_lines = []

    for i, line in enumerate(lines):
        if not header_done:
            if line.startswith("Thread ID: "):
                email["thread_id"] = line[11:].strip()
            elif line.startswith("Subject: "):
                email["subject"] = line[9:].strip()
            elif line.startswith("From: "):
                email["from"] = line[6:].strip()
            elif line.startswith("To: "):
                email["to"] = line[4:].strip()
            elif line.startswith("Date: "):
                email["date"] = line[6:].strip()
            elif line == "" and email.get("subject"):
                # Blank line after headers = start of body
                header_done = True
        else:
            body_lines.append(line)

    email["body"] = "\n".join(body_lines).strip()[:10000]
    return email


def _normalize_email(raw: dict) -> dict:
    """Normalize a parsed email dict into the standard format."""
    sender_raw = raw.get("from") or ""
    sender = sender_raw
    sender_name = ""
    if "<" in sender_raw:
        parts = sender_raw.split("<", 1)
        sender_name = parts[0].strip().strip('"')
        sender = parts[1].rstrip(">").strip()

    return {
        "id": raw.get("id", ""),
        "thread_id": raw.get("thread_id"),
        "subject": raw.get("subject") or "",
        "sender": sender,
        "sender_name": sender_name,
        "date": raw.get("date") or "",
        "snippet": raw.get("snippet") or "",
        "body": raw.get("body") or "",
        "to": raw.get("to") or "",
    }


async def search_application_emails(days: int = 14, limit: int = 100) -> list[dict]:
    """Search Gmail for application-related emails, returning metadata only.

    Runs the 3 search queries and returns normalized email dicts with
    subject, sender, date, snippet — but NO body text (no read_email calls).

    Args:
        days: Look-back period in days.
        limit: Maximum emails to return.

    Returns:
        List of normalized email dicts (body will be empty).
    """
    from mcp import ClientSession

    base = f"newer_than:{days}d"
    search_queries = [
        # ATS senders (most reliable signal)
        f"{base} {{from:noreply OR from:no-reply OR from:notifications OR from:talent OR from:careers}}",
        # Known ATS platforms
        f"{base} {{from:greenhouse.io OR from:lever.co OR from:icims.com OR from:myworkdayjobs.com OR from:jobvite.com OR from:smartrecruiters.com}}",
        # Subject-line keywords
        f"{base} {{subject:application OR subject:interview OR subject:candidate OR subject:\"next steps\" OR subject:\"your application\"}}",
        # Spam folder — job emails sometimes land here
        f"in:spam {base} {{subject:application OR subject:interview OR subject:offer OR subject:candidate}}",
    ]

    all_emails: dict[str, dict] = {}  # Deduplicate by ID

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                for query in search_queries:
                    try:
                        raw_text = await _call_tool_raw(
                            session, "search_emails",
                            {"query": query, "maxResults": limit},
                        )
                    except asyncio.TimeoutError:
                        log.warning("Gmail search timed out: %s", query[:60])
                        continue
                    except Exception as e:
                        log.warning("Gmail search failed: %s", e)
                        continue

                    results = _parse_search_results(raw_text)
                    log.info("Query returned %d results: %s", len(results), query[:80])

                    for msg in results:
                        msg_id = msg.get("id")
                        if not msg_id or msg_id in all_emails:
                            continue

                        normalized = _normalize_email(msg)
                        all_emails[msg_id] = normalized

                        if len(all_emails) >= limit:
                            break

    except Exception as e:
        log.error("Gmail MCP session failed: %s", e)
        raise

    return list(all_emails.values())


async def read_email_bodies(email_ids: list[str]) -> dict[str, dict]:
    """Batch-read full email bodies for a list of email IDs.

    Opens a single MCP session and reads all requested emails.

    Args:
        email_ids: List of Gmail message IDs to read.

    Returns:
        Dict mapping email ID to normalized email dict (with body).
    """
    if not email_ids:
        return {}

    from mcp import ClientSession

    results: dict[str, dict] = {}

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                for msg_id in email_ids:
                    try:
                        full_text = await _call_tool_raw(
                            session, "read_email",
                            {"messageId": msg_id},
                        )
                        full = _parse_read_result(full_text, msg_id)
                        results[msg_id] = _normalize_email(full)
                    except Exception as e:
                        log.debug("Could not read email %s: %s", msg_id, e)

    except Exception as e:
        log.error("Gmail MCP session failed during body read: %s", e)
        raise

    log.info("Read %d/%d email bodies", len(results), len(email_ids))
    return results


async def _get_or_create_label(session, label_name: str) -> str | None:
    """Return the Gmail label ID for label_name, creating it if missing.

    Uses the MCP server's native get_or_create_label tool.
    Returns None if label operations fail.
    """
    try:
        raw = await _call_tool_raw(session, "get_or_create_label", {"name": label_name})
        if raw.startswith("Error:"):
            log.warning("Could not get/create Gmail label '%s': %s", label_name, raw[:100])
            return None
        m = re.search(r"Label_\w+", raw)
        if m:
            return m.group(0)
        log.warning("Could not parse label ID from response: %s", raw[:100])
        return None
    except Exception as e:
        log.warning("Gmail label operation failed: %s", e)
        return None


async def apply_label_to_emails(email_ids: list[str], label: str = "ap-track") -> int:
    """Apply a Gmail label to a batch of email IDs using batch_modify_emails.

    Best-effort: gracefully skips if label operations fail.

    Args:
        email_ids: Gmail message IDs to label.
        label: Label name to apply (created if it doesn't exist).

    Returns:
        Count of emails submitted for labeling (batch operation).
    """
    if not email_ids:
        return 0

    from mcp import ClientSession

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                label_id = await _get_or_create_label(session, label)
                if label_id is None:
                    return 0

                # Chunk to avoid MCP client TaskGroup errors with large payloads
                CHUNK = 50
                total_labeled = 0
                for i in range(0, len(email_ids), CHUNK):
                    chunk = email_ids[i:i + CHUNK]
                    raw = await _call_tool_raw(session, "batch_modify_emails", {
                        "messageIds": chunk,
                        "addLabelIds": [label_id],
                    })
                    if raw.startswith("Error:"):
                        log.warning("batch_modify_emails failed (chunk %d): %s", i // CHUNK, raw[:100])
                        continue
                    total_labeled += len(chunk)

                log.info("Applied '%s' label to %d emails", label, total_labeled)
                return total_labeled

    except Exception as e:
        log.warning("Gmail label session failed: %s", e)
        return 0


async def fetch_application_emails(days: int = 14, limit: int = 100) -> list[dict]:
    """Fetch application-related emails from Gmail via MCP.

    Backward-compatible wrapper that searches and reads bodies for all emails.

    Args:
        days: Look-back period in days.
        limit: Maximum emails to return.

    Returns:
        List of normalized email dicts with body text.
    """
    emails = await search_application_emails(days=days, limit=limit)
    if not emails:
        return emails

    # Read bodies for all emails
    ids = [e["id"] for e in emails]
    bodies = await read_email_bodies(ids)

    # Merge body text into metadata emails
    for email in emails:
        if email["id"] in bodies:
            full = bodies[email["id"]]
            email["body"] = full.get("body", "")
            email["thread_id"] = full.get("thread_id") or email.get("thread_id")

    return emails
