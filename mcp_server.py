"""
mcp_server.py — Item #21
Exposes the AI Ops Copilot resolution engine as MCP (Model Context Protocol) tools.

External AI agents (Claude, GPT, custom LLMs) can discover and call these tools
via standard MCP JSON-RPC 2.0 over HTTP. Mount under FastAPI or run standalone.

Tools exposed:
    classify_incident       — classify a ticket and return resolution recommendation
    get_playbook            — fetch structured playbook steps for a classification
    get_resolution          — look up a stored resolution by classification
    list_knowledge_base     — list top classifications with stats
    get_incident_history    — episodic recall for a classification

Run standalone:
    uvicorn mcp_server:mcp_app --port 8001

Or mount into app.py:
    from mcp_server import mcp_router
    app.include_router(mcp_router, prefix="/mcp")
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

mcp_router = APIRouter(tags=["mcp"])

# ── MCP manifest ──────────────────────────────────────────────────────────────

_MANIFEST = {
    "schema_version": "2024-11",
    "name": "aiops-copilot",
    "description": "AI Ops Copilot — IT incident classification, playbook generation, and resolution knowledge base",
    "version": "1.0.0",
    "tools": [
        {
            "name": "classify_incident",
            "description": "Classify an IT incident ticket and return confidence, recommended actions, and next steps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticket_text": {"type": "string", "description": "Full incident description or ticket body"},
                    "ticket_id":   {"type": "string", "description": "Optional ticket ID for traceability"},
                    "priority":    {"type": "string", "enum": ["critical","high","medium","low"], "description": "Ticket priority"},
                },
                "required": ["ticket_text"],
            },
        },
        {
            "name": "get_playbook",
            "description": "Return structured step-by-step resolution playbook for a known incident classification.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "classification": {"type": "string", "description": "Incident classification key"},
                    "ticket_text":    {"type": "string", "description": "Optional ticket text for context"},
                },
                "required": ["classification"],
            },
        },
        {
            "name": "get_resolution",
            "description": "Look up a previously validated resolution from the knowledge base.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "classification": {"type": "string"},
                },
                "required": ["classification"],
            },
        },
        {
            "name": "list_knowledge_base",
            "description": "List top incident categories with resolution counts and success rates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
        {
            "name": "get_incident_history",
            "description": "Return episodic recall for a classification — how many times seen, average resolution time, last resolver.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "classification": {"type": "string"},
                    "limit":          {"type": "integer", "default": 5},
                },
                "required": ["classification"],
            },
        },
    ],
}


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_classify_incident(args: Dict[str, Any]) -> Dict[str, Any]:
    ticket_text = str(args.get("ticket_text") or "")
    ticket_id   = str(args.get("ticket_id") or uuid.uuid4().hex[:8])
    if not ticket_text:
        return {"error": "ticket_text is required"}
    try:
        from agents.a2a_protocol import get_dispatcher, AgentMessage
        import asyncio
        msg = AgentMessage(
            sender="mcp",
            recipient="classifier",
            intent="classify_incident",
            payload={"ticket": ticket_text, "ticket_id": ticket_id},
        )
        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(get_dispatcher().send(msg, timeout=25))
        finally:
            loop.close()
        if resp.result:
            return resp.result
    except Exception:
        pass
    # Fallback: direct classification call
    try:
        import requests as _req
        import os
        backend = os.getenv("BACKEND_URL", "http://localhost:8000")
        r = _req.post(f"{backend}/analyze",
                      json={"ticket": ticket_text, "ticket_id": ticket_id}, timeout=25)
        if r.ok:
            return r.json()
    except Exception as exc:
        logger.warning("mcp classify_incident fallback failed: %s", exc)
    return {"error": "classification unavailable"}


def _tool_get_playbook(args: Dict[str, Any]) -> Dict[str, Any]:
    classification = str(args.get("classification") or "").strip().lower()
    ticket_text    = str(args.get("ticket_text") or "")
    if not classification:
        return {"error": "classification is required"}
    try:
        from agents.playbook_agent import generate_playbook
        steps = generate_playbook(classification, ticket=ticket_text)
        return {"classification": classification, "steps": steps, "count": len(steps)}
    except Exception as exc:
        logger.warning("mcp get_playbook failed: %s", exc)
        return {"error": str(exc)}


def _tool_get_resolution(args: Dict[str, Any]) -> Dict[str, Any]:
    classification = str(args.get("classification") or "").strip().lower()
    if not classification:
        return {"error": "classification is required"}
    try:
        from knowledge_manager import find_resolution_by_classification
        res = find_resolution_by_classification(classification)
        if res:
            return {"found": True, "resolution": res}
        return {"found": False, "classification": classification}
    except Exception as exc:
        return {"error": str(exc)}


def _tool_list_knowledge_base(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 10)
    try:
        from long_term_memory import list_top_classifications
        rows = list_top_classifications(limit=limit)
        return {"categories": rows, "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}


def _tool_get_incident_history(args: Dict[str, Any]) -> Dict[str, Any]:
    classification = str(args.get("classification") or "").strip().lower()
    limit = int(args.get("limit") or 5)
    if not classification:
        return {"error": "classification is required"}
    try:
        from episodic_memory import get_episodes
        from long_term_memory import get_resolution_summary
        episodes = get_episodes(classification, limit=limit)
        summary  = get_resolution_summary(classification)
        return {
            "classification": classification,
            "summary": summary,
            "episodes": episodes,
            "total_episodes": len(episodes),
        }
    except Exception as exc:
        return {"error": str(exc)}


_TOOL_HANDLERS = {
    "classify_incident":    _tool_classify_incident,
    "get_playbook":         _tool_get_playbook,
    "get_resolution":       _tool_get_resolution,
    "list_knowledge_base":  _tool_list_knowledge_base,
    "get_incident_history": _tool_get_incident_history,
}


# ── MCP JSON-RPC endpoints ────────────────────────────────────────────────────

@mcp_router.get("/manifest")
def get_manifest() -> Dict[str, Any]:
    """Return the MCP tool manifest so AI agents can discover available tools."""
    return _MANIFEST


@mcp_router.post("/call")
async def call_tool(request: Request) -> JSONResponse:
    """
    MCP tool call endpoint — JSON-RPC 2.0 compatible.
    Body: {"tool": "<name>", "arguments": {...}}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    tool_name = str(body.get("tool") or body.get("name") or "")
    arguments  = body.get("arguments") or body.get("params") or {}
    request_id = body.get("id")

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        resp: Dict[str, Any] = {
            "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"},
            "available_tools": list(_TOOL_HANDLERS.keys()),
        }
        if request_id is not None:
            resp["id"] = request_id
        return JSONResponse(resp, status_code=404)

    try:
        result = handler(arguments)
        resp = {"result": result}
        if request_id is not None:
            resp["id"] = request_id
        return JSONResponse(resp)
    except Exception as exc:
        logger.exception("MCP tool '%s' raised: %s", tool_name, exc)
        resp = {"error": {"code": -32000, "message": str(exc)}}
        if request_id is not None:
            resp["id"] = request_id
        return JSONResponse(resp, status_code=500)


@mcp_router.get("/tools")
def list_tools() -> Dict[str, Any]:
    """List all available MCP tools with their names and descriptions."""
    return {
        "tools": [
            {"name": t["name"], "description": t["description"]}
            for t in _MANIFEST["tools"]
        ]
    }


# ── MCP SSE transport (proper MCP protocol — Claude Desktop / MCP clients) ───
# The MCP spec uses Server-Sent Events for streaming and a POST endpoint for
# client messages. This implements the minimal handshake so Claude Desktop
# and other MCP clients can connect.

from fastapi.responses import StreamingResponse
import asyncio


@mcp_router.get("/sse")
async def mcp_sse(request: Request) -> StreamingResponse:
    """
    MCP SSE endpoint — clients connect here to receive the endpoint URL.
    Complies with MCP 2024-11 transport spec: sends endpoint event then keeps alive.
    """
    async def _event_stream():
        # Send the messages endpoint URL
        base = str(request.base_url).rstrip("/")
        yield f"event: endpoint\ndata: {base}/mcp/messages\n\n"
        # Keep-alive ping every 15 seconds
        while True:
            await asyncio.sleep(15)
            if await request.is_disconnected():
                break
            yield ": ping\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@mcp_router.post("/messages")
async def mcp_messages(request: Request) -> JSONResponse:
    """
    MCP messages endpoint — receives JSON-RPC 2.0 messages from MCP clients.
    Handles initialize, tools/list, and tools/call methods.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})

    method = str(body.get("method") or "")
    req_id = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aiops-copilot", "version": "1.0.0"},
            },
        })

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _MANIFEST["tools"]},
        })

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        arguments  = params.get("arguments") or {}
        handler    = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
            })
        try:
            result = handler(arguments)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    "isError": "error" in result,
                },
            })
        except Exception as exc:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            })

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


# ── Standalone app (uvicorn mcp_server:mcp_app --port 8001) ──────────────────
mcp_app = FastAPI(title="AI Ops Copilot MCP Server", version="1.0.0")
mcp_app.include_router(mcp_router, prefix="/mcp")
