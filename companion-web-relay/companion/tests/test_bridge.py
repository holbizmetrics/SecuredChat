from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from companion.bridge import (  # noqa: E402
    BridgeError,
    Config,
    DeliveryStore,
    IncomingClaims,
    SecuredChatCli,
    make_handler,
    main,
    valid_identity,
)
from http.server import ThreadingHTTPServer  # noqa: E402


class FakeCli:
    def __init__(self):
        self.acks: list[tuple[str, str]] = []
        self.sends: list[tuple[str, str, str, str]] = []

    def poll(self, identity: str) -> list[dict]:
        return [{
            "id": "msg-1", "from": "windows-claude", "to": identity,
            "kind": "msg", "body": "hello",
        }]

    def acknowledge(self, identity: str, message_id: str) -> None:
        self.acks.append((identity, message_id))

    def send(self, identity: str, to: str, reply_to: str, body: str) -> dict:
        self.sends.append((identity, to, reply_to, body))
        return {"id": "reply-1", "status": "sent"}


class BridgeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeCli()
        config = Config(
            chat_py=Path(self.temp.name) / "chat.py",
            bus=Path(self.temp.name) / "bus",
            room="test-room",
            token="t" * 40,
        )
        store = DeliveryStore(Path(self.temp.name) / "deliveries.json")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, self.fake, store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path: str, *, token: bool = True, origin: str | None = "chrome-extension://abc",
                payload: dict | None = None) -> tuple[int, dict]:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {'t' * 40}"
        if origin:
            headers["Origin"] = origin
        body = None
        method = "GET"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            return error.code, json.loads(error.read())
        return response.status, json.loads(response.read())

    def test_health_requires_token(self) -> None:
        status, payload = self.request("/v1/health", token=False)
        self.assertEqual(status, 401)
        self.assertIn("token", payload["error"])

    def test_health_reports_a_process_instance_id(self) -> None:
        """Codex re-review of extension ee7f870, defect 3.

        The extension binds its Mode-2 arming window to the companion instance it was armed
        against. Without this field a companion that restarts between two polls is
        indistinguishable from one that never went away, and an armed AUTO window survives a
        real restart. Version and room are deliberately NOT sufficient: the whole point of the
        defect is that they are identical across a restart.
        """
        _, first = self.request("/v1/health")
        self.assertIn("instance", first)
        self.assertIsInstance(first["instance"], str)
        self.assertGreaterEqual(len(first["instance"]), 16)

        # Stable within one process: an unchanged companion must not look like a restart on
        # every poll, or the extension would disarm continuously.
        _, second = self.request("/v1/health")
        self.assertEqual(first["instance"], second["instance"])

        # And it is not derived from version/room, which are identical across a restart.
        self.assertNotEqual(first["instance"], first["version"])
        self.assertNotEqual(first["instance"], first["room"])

    def test_instance_id_differs_across_real_processes(self) -> None:
        """Two genuinely separate interpreter processes must not share an instance id.

        Deliberately a subprocess rather than importlib.reload(): reloading swaps the module's
        classes underneath the other tests in this file, and the first version of this test broke
        two of them. A restart is a new PROCESS, so the test uses a new process.
        """
        root = Path(__file__).resolve().parents[2]
        program = "import companion.bridge as b; print(b.INSTANCE_ID)"
        ids = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True, text=True, cwd=str(root), timeout=30, check=True,
            )
            ids.append(result.stdout.strip())

        self.assertNotEqual(
            ids[0], ids[1],
            "a fresh companion process must produce a fresh instance id; a constant would make "
            "every restart invisible to the extension again",
        )
        for value in ids:
            self.assertEqual(len(value), 32)
            int(value, 16)  # hex, no separators

    def test_health_and_poll(self) -> None:
        status, payload = self.request("/v1/health")
        self.assertEqual((status, payload["status"], payload["room"]), (200, "ready", "test-room"))
        status, payload = self.request("/v1/poll?identity=chatgpt-web&client_id=tab-one")
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"][0]["body"], "hello")

    def test_rejects_web_page_origins(self) -> None:
        status, payload = self.request("/v1/health", origin="https://chatgpt.com")
        self.assertEqual(status, 400)
        self.assertIn("origin", payload["error"])

    def test_acknowledges_message(self) -> None:
        status, payload = self.request("/v1/ack", payload={
            "identity": "chatgpt-web", "message_id": "msg-1", "client_id": "tab-one",
        })
        self.assertEqual((status, payload["status"]), (200, "acknowledged"))
        self.assertEqual(self.fake.acks, [("chatgpt-web", "msg-1")])

    def test_send_is_idempotent(self) -> None:
        data = {
            "identity": "chatgpt-web", "to": "windows-claude", "reply_to": "msg-1",
            "body": "reply", "delivery_id": "chatgpt:msg-1",
        }
        first_status, first = self.request("/v1/send", payload=data)
        second_status, second = self.request("/v1/send", payload=data)
        self.assertEqual((first_status, first["idempotent"]), (200, False))
        self.assertEqual((second_status, second["idempotent"]), (200, True))
        self.assertEqual(len(self.fake.sends), 1)

    def test_invalid_identity_fails_closed(self) -> None:
        status, payload = self.request("/v1/ack", payload={
            "identity": "bad identity", "message_id": "msg-1", "client_id": "tab-one",
        })
        self.assertEqual(status, 400)
        self.assertIn("identity", payload["error"])


class CliContractTests(unittest.TestCase):
    def test_identity_validation(self) -> None:
        self.assertEqual(valid_identity("claude-web_1"), "claude-web_1")
        with self.assertRaises(BridgeError):
            valid_identity("../../escape")

    def test_json_line_filtering(self) -> None:
        output = 'noise\n{"id":"one","body":"a"}\n[]\n{"id":"two"}\n'
        self.assertEqual([row["id"] for row in SecuredChatCli._json_lines(output)], ["one", "two"])

    def test_command_is_argument_vector_not_shell(self) -> None:
        config = Config(Path("/tmp/chat.py"), Path("/tmp/bus"), "room", "t" * 40)
        cli = SecuredChatCli(config)
        command = cli._base("chatgpt-web")
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], "chatgpt-web")
        self.assertIn("--bus", command)

    def test_signature_attestation_is_only_claimed_in_strict_mode(self) -> None:
        class StubCli(SecuredChatCli):
            def _run(self, identity: str, args: list[str]) -> str:
                return json.dumps({
                    "id": "one", "from": "sender", "to": identity,
                    "kind": "msg", "body": "hello",
                }) + "\n"

        strict = StubCli(Config(Path("chat.py"), Path("bus"), "room", "t" * 40, verify_sig="strict"))
        warn = StubCli(Config(Path("chat.py"), Path("bus"), "room", "t" * 40, verify_sig="warn"))
        self.assertEqual(strict.poll("chatgpt-web")[0]["signature_status"], "verified")
        self.assertEqual(warn.poll("chatgpt-web")[0]["signature_status"], "not-attested")

    def test_different_identities_serialize_operations_on_one_bus_clone(self) -> None:
        class StubCli(SecuredChatCli):
            def __init__(self, config: Config):
                super().__init__(config)
                self.guard = threading.Lock()
                self.active = 0
                self.maximum_active = 0
                self.run_count = 0

            def _run(self, identity: str, args: list[str]) -> str:
                with self.guard:
                    self.active += 1
                    self.run_count += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                time.sleep(0.05)
                with self.guard:
                    self.active -= 1
                return ""

        cli = StubCli(Config(Path("chat.py"), Path("bus"), "room", "t" * 40))
        threads = [
            threading.Thread(target=cli.acknowledge, args=("chatgpt-web", "one")),
            threading.Thread(target=cli.acknowledge, args=("claude-web", "two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(cli.run_count, 2)
        self.assertEqual(cli.maximum_active, 1)

    def test_busy_bus_poll_returns_without_queuing(self) -> None:
        cli = SecuredChatCli(Config(Path("chat.py"), Path("bus"), "room", "t" * 40))
        lock = cli._lock("chatgpt-web")
        lock.acquire()
        try:
            started = time.monotonic()
            self.assertEqual(cli.poll("claude-web"), [])
            self.assertLess(time.monotonic() - started, 0.75)
        finally:
            lock.release()

    def test_check_does_not_create_or_consume_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chat_py = root / "chat.py"
            chat_py.write_text("import sys\nraise SystemExit(0)\n", encoding="utf-8")
            bus = root / "bus"
            bus.mkdir()
            token_file = root / "bridge-token.json"
            result = main([
                "--chat-py", str(chat_py), "--bus", str(bus),
                "--token-file", str(token_file), "--check",
            ])
            self.assertEqual(result, 0)
            self.assertFalse(token_file.exists())


class IncomingClaimTests(unittest.TestCase):
    def test_second_tab_cannot_claim_same_identity(self) -> None:
        claims = IncomingClaims()
        messages = [{"id": "one", "body": "hello"}]
        self.assertEqual(claims.claim("chatgpt-web", "tab-one", messages), messages)
        self.assertEqual(claims.claim("chatgpt-web", "tab-two", messages), [])
        with self.assertRaises(BridgeError):
            claims.verify_owner("chatgpt-web", "tab-two", "one")
        claims.release("chatgpt-web", "tab-one", "one")
        self.assertEqual(claims.claim("chatgpt-web", "tab-two", messages), messages)


class ResponseDisconnectTests(unittest.TestCase):
    def test_client_disconnect_during_response_is_quiet(self) -> None:
        class BrokenWriter:
            def write(self, _body: bytes) -> None:
                raise ConnectionAbortedError(10053, "client closed")

        config = Config(Path("chat.py"), Path("bus"), "room", "t" * 40)
        handler_type = make_handler(config, FakeCli(), DeliveryStore(Path("unused.json")))
        handler = object.__new__(handler_type)
        handler._headers = lambda _status, _origin=None: None
        handler.send_header = lambda _name, _value: None
        handler.end_headers = lambda: None
        handler.wfile = BrokenWriter()
        self.assertFalse(handler._json(200, {"status": "ready"}))


if __name__ == "__main__":
    unittest.main()
