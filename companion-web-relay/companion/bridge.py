#!/usr/bin/env python3
"""Localhost-only HTTP bridge between browser surfaces and SecuredChat CLI."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.2"

# Per-PROCESS instance identifier, generated once at import and never reused.
#
# Codex re-review of extension commit ee7f870, defect 3: the browser extension binds its Mode-2
# arming window to the companion it was armed against, but /v1/health carried no instance
# identifier. A companion that restarted completely between two extension polls returned
# identical status, version, room and securedchat fields, so the extension could not distinguish
# a restart from an uninterrupted process -- and an armed AUTO window survived a real restart,
# which the bridge's own control table forbids.
#
# No client-side heuristic closes that gap; the companion has to say who it is. This value
# changes on every start, so the extension can invalidate arming the moment it changes.
INSTANCE_ID = secrets.token_hex(16)
IDENTITY_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
MAX_REQUEST_BYTES = 300_000
MAX_MESSAGE_BYTES = 200_000
ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")


class BridgeError(RuntimeError):
    """An expected request or SecuredChat CLI failure."""


@dataclass(frozen=True)
class Config:
    chat_py: Path
    bus: Path
    room: str
    token: str
    verify_sig: str = "warn"
    cli_timeout: float = 30.0


def valid_identity(value: object) -> str:
    value = str(value or "")
    if not IDENTITY_RE.fullmatch(value):
        raise BridgeError("identity must match [A-Za-z0-9._-] and be at most 96 characters")
    return value


class SecuredChatCli:
    """Small, shell-free adapter around the existing chat.py contract."""

    def __init__(self, config: Config):
        self.config = config
        # Every identity operates on the same local Git clone. Git operations
        # must therefore be serialized per clone, not merely per identity.
        self._transport_lock = threading.Lock()

    def _lock(self, identity: str) -> threading.Lock:
        valid_identity(identity)
        return self._transport_lock

    def _base(self, identity: str) -> list[str]:
        return [
            sys.executable,
            str(self.config.chat_py),
            "--bus", str(self.config.bus),
            "--room", self.config.room,
            "--identity", valid_identity(identity),
        ]

    def _run(self, identity: str, args: list[str]) -> str:
        command = self._base(identity) + args
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.cli_timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"SecuredChat CLI could not run: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown CLI failure").strip()[-1200:]
            raise BridgeError(f"SecuredChat CLI exited {result.returncode}: {detail}")
        return result.stdout

    @staticmethod
    def _json_lines(output: str) -> list[dict]:
        messages: list[dict] = []
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("id"):
                messages.append(value)
        return messages

    def poll(self, identity: str) -> list[dict]:
        identity = valid_identity(identity)
        # Polls are speculative and repeat. Do not build a queue of browser
        # requests behind a slow Git pull; a busy poll simply retries later.
        lock = self._lock(identity)
        if not lock.acquire(timeout=0.25):
            return []
        try:
            output = self._run(identity, [
                "recv", "--addressed-to-me", "--exclude-self", "--json",
                "--verify-sig", self.config.verify_sig,
            ])
        finally:
            lock.release()
        result = []
        for message in self._json_lines(output):
            if message.get("kind", "msg") != "msg":
                continue
            body = str(message.get("body") or "")
            if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
                continue
            result.append({
                "ts": message.get("ts"),
                "id": str(message["id"]),
                "from": str(message.get("from") or "unknown"),
                "to": message.get("to"),
                "kind": "msg",
                "body": body,
                "reply_to": message.get("reply_to"),
                # chat.py's strict policy drops every non-verified row before its
                # JSON output. Warn/off output does not carry a per-row verdict,
                # so do not invent one.
                "signature_status": ("verified" if self.config.verify_sig == "strict"
                                     else "not-attested"),
            })
        return result[:5]

    def acknowledge(self, identity: str, message_id: str) -> None:
        identity = valid_identity(identity)
        if not message_id or len(message_id) > 128:
            raise BridgeError("invalid message_id")
        with self._lock(identity):
            self._run(identity, ["mark-seen", message_id])

    def send(self, identity: str, to: str, reply_to: str, body: str) -> dict:
        identity = valid_identity(identity)
        to = valid_identity(to)
        if not reply_to or len(reply_to) > 128:
            raise BridgeError("invalid reply_to")
        if not body.strip():
            raise BridgeError("reply body is empty")
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BridgeError("reply body is too large")
        with self._lock(identity):
            output = self._run(identity, [
                "send", body, "--to", to, "--reply-to", reply_to, "--json",
            ])
        rows = self._json_lines(output)
        return rows[-1] if rows else {"status": "sent", "raw": output.strip()[-500:]}


class DeliveryStore:
    """Persistent at-most-once reservations for web replies."""

    def __init__(self, path: Path, limit: int = 1000):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"deliveries": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"deliveries": {}}
        if not isinstance(value, dict) or not isinstance(value.get("deliveries"), dict):
            return {"deliveries": {}}
        return value

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, delivery_id: str) -> dict | None:
        with self._lock:
            return self._read()["deliveries"].get(delivery_id)

    def reserve(self, delivery_id: str) -> dict:
        if not delivery_id or len(delivery_id) > 220:
            raise BridgeError("invalid delivery_id")
        with self._lock:
            value = self._read()
            existing = value["deliveries"].get(delivery_id)
            if existing:
                return existing
            row = {"status": "reserved", "ts": time.time()}
            value["deliveries"][delivery_id] = row
            self._trim(value)
            self._write(value)
            return row

    def complete(self, delivery_id: str, result: dict) -> dict:
        with self._lock:
            value = self._read()
            row = {"status": "sent", "ts": time.time(), "result": result}
            value["deliveries"][delivery_id] = row
            self._trim(value)
            self._write(value)
            return row

    def release_reservation(self, delivery_id: str) -> None:
        with self._lock:
            value = self._read()
            if value["deliveries"].get(delivery_id, {}).get("status") == "reserved":
                value["deliveries"].pop(delivery_id, None)
                self._write(value)

    def _trim(self, value: dict) -> None:
        rows = value["deliveries"]
        if len(rows) <= self.limit:
            return
        ordered = sorted(rows, key=lambda key: float(rows[key].get("ts") or 0))
        for key in ordered[:len(rows) - self.limit]:
            rows.pop(key, None)


class IncomingClaims:
    """Serialize delivery to browser tabs sharing one SecuredChat identity."""

    def __init__(self, ttl_seconds: float = 1800.0):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._claims: dict[str, dict] = {}

    def claim(self, identity: str, client_id: str, messages: list[dict]) -> list[dict]:
        identity = valid_identity(identity)
        client_id = valid_identity(client_id)
        now = time.time()
        with self._lock:
            row = self._claims.get(identity)
            if row and float(row.get("expires") or 0) <= now:
                self._claims.pop(identity, None)
                row = None
            if row:
                if row["client_id"] != client_id:
                    return []
                match = next((message for message in messages if message["id"] == row["message_id"]), None)
                return [match] if match else []
            if not messages:
                return []
            message = messages[0]
            self._claims[identity] = {
                "client_id": client_id,
                "message_id": message["id"],
                "expires": now + self.ttl_seconds,
            }
            return [message]

    def release(self, identity: str, client_id: str, message_id: str) -> None:
        identity = valid_identity(identity)
        client_id = valid_identity(client_id)
        with self._lock:
            row = self._claims.get(identity)
            if not row:
                return
            if row["client_id"] != client_id or row["message_id"] != message_id:
                raise BridgeError("incoming message is claimed by another browser tab")
            self._claims.pop(identity, None)

    def verify_owner(self, identity: str, client_id: str, message_id: str) -> None:
        identity = valid_identity(identity)
        client_id = valid_identity(client_id)
        with self._lock:
            row = self._claims.get(identity)
            if row and (row["client_id"] != client_id or row["message_id"] != message_id):
                raise BridgeError("incoming message is claimed by another browser tab")


def make_handler(config: Config, cli: SecuredChatCli, store: DeliveryStore):
    claims = IncomingClaims()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"SecuredChatWebRelay/{VERSION}"

        def log_message(self, fmt: str, *args) -> None:
            print(f"[web-relay] {self.address_string()} {fmt % args}", file=sys.stderr)

        def _origin(self) -> str | None:
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(ALLOWED_ORIGIN_PREFIXES):
                raise BridgeError("origin is not an installed browser extension")
            return origin

        def _headers(self, status: int, origin: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _json(self, status: int, payload: dict, origin: str | None = None) -> bool:
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._headers(status, origin)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # A browser navigation, extension timeout, or shutdown may
                # close the socket while a response is being written. There
                # is no peer left to receive an error response, so stop here.
                return False

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {config.token}"
            return hmac.compare_digest(supplied, expected)

        def _body(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise BridgeError("invalid Content-Length") from exc
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise BridgeError("request body is empty or too large")
            try:
                value = json.loads(self.rfile.read(size))
            except json.JSONDecodeError as exc:
                raise BridgeError("request body is not JSON") from exc
            if not isinstance(value, dict):
                raise BridgeError("request body must be a JSON object")
            return value

        def _guard(self) -> str | None:
            origin = self._origin()
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"}, origin)
                return None
            return origin or ""

        def do_OPTIONS(self) -> None:  # noqa: N802
            try:
                origin = self._origin()
                self.send_response(HTTPStatus.NO_CONTENT)
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()
            except BridgeError as exc:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            origin: str | None = None
            try:
                guarded = self._guard()
                if guarded is None:
                    return
                origin = guarded or None
                parsed = urlparse(self.path)
                if parsed.path == "/v1/health":
                    self._json(HTTPStatus.OK, {
                        "status": "ready", "version": VERSION, "securedchat": "cli",
                        "room": config.room,
                        # Changes on every companion start. The extension binds Mode-2 arming to
                        # this exact value and disarms whenever it changes.
                        "instance": INSTANCE_ID,
                    }, origin)
                    return
                if parsed.path == "/v1/poll":
                    query = parse_qs(parsed.query)
                    identity = valid_identity(query.get("identity", [""])[0])
                    client_id = valid_identity(query.get("client_id", [""])[0])
                    self._json(HTTPStatus.OK, {
                        "messages": claims.claim(identity, client_id, cli.poll(identity))
                    }, origin)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"}, origin)
            except BridgeError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, origin)
            except Exception as exc:  # fail closed without exposing a traceback over HTTP
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"bridge failure: {exc}"}, origin)

        def do_POST(self) -> None:  # noqa: N802
            origin: str | None = None
            try:
                guarded = self._guard()
                if guarded is None:
                    return
                origin = guarded or None
                data = self._body()
                parsed = urlparse(self.path)
                if parsed.path == "/v1/ack":
                    identity = valid_identity(data.get("identity"))
                    client_id = valid_identity(data.get("client_id"))
                    message_id = str(data.get("message_id") or "")
                    claims.verify_owner(identity, client_id, message_id)
                    cli.acknowledge(identity, message_id)
                    claims.release(identity, client_id, message_id)
                    self._json(HTTPStatus.OK, {"status": "acknowledged"}, origin)
                    return
                if parsed.path == "/v1/send":
                    delivery_id = str(data.get("delivery_id") or "")
                    existing = store.get(delivery_id)
                    if existing:
                        self._json(HTTPStatus.OK, {"status": existing["status"], "idempotent": True,
                                                   "result": existing.get("result")}, origin)
                        return
                    store.reserve(delivery_id)
                    try:
                        result = cli.send(
                            valid_identity(data.get("identity")),
                            valid_identity(data.get("to")),
                            str(data.get("reply_to") or ""),
                            str(data.get("body") or ""),
                        )
                    except Exception:
                        store.release_reservation(delivery_id)
                        raise
                    store.complete(delivery_id, result)
                    self._json(HTTPStatus.OK, {"status": "sent", "idempotent": False,
                                               "result": result}, origin)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"}, origin)
            except BridgeError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, origin)
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"bridge failure: {exc}"}, origin)

    return Handler


def load_or_create_token(path: Path) -> tuple[str, bool]:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            token = str(value.get("token") or "")
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Cannot read token file {path}: {exc}") from exc
        if len(token) < 32:
            raise SystemExit(f"Token in {path} is missing or too short")
        return token, False
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": token}, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token, True


def parser() -> argparse.ArgumentParser:
    default_config = Path.home() / ".config" / "securedchat-web-relay" / "bridge-token.json"
    default_state = Path.home() / ".config" / "securedchat-web-relay" / "delivery-state.json"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chat-py", type=Path,
                    default=os.environ.get("SECUREDCHAT_CHAT_PY"),
                    help="path to SecuredChat cli/chat.py")
    ap.add_argument("--bus", type=Path, default=os.environ.get("SECUREDCHAT_BUS"),
                    help="path to the local SecuredChat bus clone")
    ap.add_argument("--room", default=os.environ.get("SECUREDCHAT_ROOM", "prometheus-relay"))
    ap.add_argument("--token", default=os.environ.get("SECUREDCHAT_WEB_RELAY_TOKEN"))
    ap.add_argument("--token-file", type=Path, default=default_config)
    ap.add_argument("--state-file", type=Path, default=default_state)
    ap.add_argument("--verify-sig", choices=["off", "warn", "strict"], default="warn")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--check", action="store_true", help="validate configuration and exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.chat_py or not args.bus:
        print("--chat-py and --bus are required (or set SECUREDCHAT_CHAT_PY / SECUREDCHAT_BUS)", file=sys.stderr)
        return 2
    chat_py = Path(args.chat_py).expanduser().resolve()
    bus = Path(args.bus).expanduser().resolve()
    if not chat_py.is_file():
        print(f"SecuredChat CLI not found: {chat_py}", file=sys.stderr)
        return 2
    if not bus.is_dir():
        print(f"SecuredChat bus not found: {bus}", file=sys.stderr)
        return 2
    if args.check:
        # A configuration check must not consume the one-time token display.
        # The token is not used by the CLI validation call.
        config = Config(chat_py=chat_py, bus=bus, room=args.room,
                        token="check-only-token-not-used-000000", verify_sig=args.verify_sig)
        cli = SecuredChatCli(config)
        cli._run("web-relay-check", ["--help"])
        print(json.dumps({"status": "ready", "version": VERSION, "room": args.room,
                          "instance": INSTANCE_ID}))
        return 0
    token, created = (args.token, False) if args.token else load_or_create_token(args.token_file)
    if len(token) < 32:
        print("Bridge token must contain at least 32 characters", file=sys.stderr)
        return 2
    config = Config(chat_py=chat_py, bus=bus, room=args.room, token=token, verify_sig=args.verify_sig)
    cli = SecuredChatCli(config)
    if created:
        print("NEW BRIDGE TOKEN — copy this once into the extension popup:")
        print(token)
    else:
        print(f"Bridge token loaded; token file: {args.token_file}")
    handler = make_handler(config, cli, DeliveryStore(args.state_file))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"SecuredChat Web Relay v{VERSION} listening on http://127.0.0.1:{args.port}")
    print(f"Room: {args.room} · Ctrl+C stops the bridge")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping bridge")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
