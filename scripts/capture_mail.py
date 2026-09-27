"""Bounded local SMTP capture; never authenticates, relays, or contacts another server."""

from __future__ import annotations

import argparse
import re
import socketserver
import threading
from pathlib import Path
from uuid import uuid4

MAILBOX_ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "mailbox"
_ENVELOPE = re.compile(rb"<(?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+)@(?:[A-Za-z0-9-]+\.)+invalid>", re.I)


class CaptureHandler(socketserver.StreamRequestHandler):
    server: CaptureServer

    def _reply(self, response: bytes) -> None:
        self.wfile.write(response + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        self.connection.settimeout(10)
        try:
            self._conversation()
        except (OSError, ValueError):
            # A closed/timed-out local connection does not reveal envelope or message content.
            return

    def _conversation(self) -> None:
        self._reply(b"220 capture.invalid SMTP capture")
        sender = recipient = False
        greeted = False
        while True:
            raw = self.rfile.readline(1025)
            if not raw:
                return
            if len(raw) > 1024 or not raw.endswith(b"\r\n"):
                self._reply(b"500 Invalid command")
                return
            line = raw[:-2]
            command, _, value = line.partition(b" ")
            command = command.upper()
            if command in (b"EHLO", b"HELO"):
                greeted, sender, recipient = True, False, False
                self._reply(
                    b"250-capture.invalid\r\n250 SIZE " + str(self.server.max_bytes).encode()
                )
            elif command == b"MAIL":
                sender = recipient = False
                address = value[5:] if value.upper().startswith(b"FROM:") else b""
                sender = greeted and _ENVELOPE.fullmatch(address) is not None
                self._reply(
                    b"250 Sender accepted"
                    if sender
                    else b"550 Local invalid-domain sender required"
                )
            elif command == b"RCPT":
                address = value[3:] if value.upper().startswith(b"TO:") else b""
                accepted = sender and not recipient and _ENVELOPE.fullmatch(address) is not None
                if accepted:
                    recipient = True
                self._reply(
                    b"250 Recipient accepted"
                    if accepted
                    else b"550 Local invalid-domain recipient required"
                )
            elif command == b"DATA":
                if not sender or not recipient or value:
                    self._reply(b"503 Envelope required")
                    continue
                self._reply(b"354 End data with a single dot")
                data = bytearray()
                while True:
                    part = self.rfile.readline(self.server.max_bytes + 1)
                    if not part:
                        return
                    if part == b".\r\n":
                        break
                    if part.startswith(b".."):
                        part = part[1:]
                    if len(data) + len(part) > self.server.max_bytes:
                        self._reply(b"552 Message too large")
                        return
                    data.extend(part)
                sender = recipient = False
                if self.server.capture(bytes(data)):
                    self._reply(b"250 Captured")
                else:
                    self._reply(b"452 Capture capacity reached")
            elif command == b"RSET":
                sender = recipient = False
                self._reply(b"250 Reset")
            elif command == b"NOOP":
                self._reply(b"250 OK")
            elif command == b"QUIT":
                self._reply(b"221 Closing")
                return
            else:
                self._reply(b"502 Unsupported command")


class CaptureServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int] = ("127.0.0.1", 1025),
        *,
        mailbox: Path = MAILBOX_ROOT,
        max_bytes: int = 65_536,
        max_messages: int = 100,
        handler: type[CaptureHandler] = CaptureHandler,
    ) -> None:
        if address[0] != "127.0.0.1" or not 0 <= address[1] <= 65535:
            raise ValueError("Capture binds only the fixed IPv4 loopback address")
        mailbox = mailbox.resolve()
        if not mailbox.is_relative_to(MAILBOX_ROOT.resolve()):
            raise ValueError("Capture files must stay inside the ignored project mailbox")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 65_536:
            raise ValueError("Message limit must be 1024 to 65536 bytes")
        if type(max_messages) is not int or not 1 <= max_messages <= 1000:
            raise ValueError("Capture capacity must be 1 to 1000 messages")
        mailbox.mkdir(parents=True, exist_ok=True)
        self.mailbox, self.max_bytes, self.max_messages = mailbox, max_bytes, max_messages
        self._lock = threading.Lock()
        self._connections = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def capture(self, data: bytes) -> bool:
        with self._lock:
            if (
                len(data) > self.max_bytes
                or sum(1 for _ in self.mailbox.glob("*.eml")) >= self.max_messages
            ):
                return False
            # A fresh local receipt file preserves duplicates; Message-ID does not deduplicate SMTP.
            with (self.mailbox / (uuid4().hex + ".eml")).open("xb") as target:
                target.write(data)
            return True

    def process_request(self, request, client_address) -> None:
        if not self._connections.acquire(blocking=False):
            try:
                request.sendall(b"421 Capture busy\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request, client_address) -> None:
        # Capture is a development sink; no traceback may print message content or client input.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=1025)
    parser.add_argument("--max-messages", type=int, default=100)
    args = parser.parse_args()
    with CaptureServer(("127.0.0.1", args.port), max_messages=args.max_messages) as server:
        print(f"Local SMTP capture: 127.0.0.1:{server.server_address[1]}")
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
