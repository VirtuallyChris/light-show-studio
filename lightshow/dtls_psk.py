"""A small DTLS 1.2 client for one cipher suite: TLS_PSK_WITH_AES_128_GCM_SHA256 (RFC 4279, 5288, 6347).

The Hue bridge streams light colors only over DTLS 1.2 with a pre-shared key and this suite. The
python-mbedtls package that wraps it has no wheels for current Pythons, so this module speaks the
protocol itself; AES-GCM comes from pycryptodomex. It does the client handshake (with the cookie
exchange), encrypted application data and close_notify. It does not do renegotiation, resumption or
certificates, and it ignores anything the server sends after the handshake.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time

from Cryptodome.Cipher import AES

VERSION = b"\xfe\xfd"                 # DTLS 1.2
SUITE = 0x00A8                        # TLS_PSK_WITH_AES_128_GCM_SHA256

CHANGE_CIPHER_SPEC, ALERT, HANDSHAKE, APPLICATION_DATA = 20, 21, 22, 23
CLIENT_HELLO, SERVER_HELLO, HELLO_VERIFY_REQUEST = 1, 2, 3
SERVER_KEY_EXCHANGE, SERVER_HELLO_DONE, CLIENT_KEY_EXCHANGE, FINISHED = 12, 14, 16, 20


class DtlsError(Exception):
    pass


def prf(secret: bytes, label: bytes, seed: bytes, n: int) -> bytes:
    """The TLS 1.2 PRF with SHA-256 (RFC 5246 section 5)."""
    seed = label + seed
    out, a = b"", seed
    while len(out) < n:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:n]


def psk_premaster(psk: bytes) -> bytes:
    """RFC 4279: as many zeros as the key is long, then the key, each with its length."""
    n = struct.pack(">H", len(psk))
    return n + bytes(len(psk)) + n + psk


def _u24(n: int) -> bytes:
    return n.to_bytes(3, "big")


def _handshake(kind: int, seq: int, body: bytes) -> bytes:
    """One whole handshake message with its DTLS header (also what goes into the transcript)."""
    return bytes([kind]) + _u24(len(body)) + struct.pack(">H", seq) + _u24(0) + _u24(len(body)) + body


class DtlsPskClient:
    def __init__(self, host: str, port: int, identity: bytes, psk: bytes, timeout: float = 1.0, tries: int = 5):
        self.addr = (host, port)
        self.identity, self.psk = identity, psk
        self.timeout, self.tries = timeout, tries
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self.epoch = 0
        self.seqs = {0: 0, 1: 0}      # the next record number in each epoch
        self.msg_seq = 0              # handshake message sequence
        self.write_key = self.write_salt = None
        self.read_key = self.read_salt = None
        self.connected = False

    # -- records -------------------------------------------------------------
    def _record(self, kind: int, payload: bytes, epoch: int | None = None) -> bytes:
        epoch = self.epoch if epoch is None else epoch
        seq = struct.pack(">H", epoch) + self.seqs[epoch].to_bytes(6, "big")
        self.seqs[epoch] += 1
        if epoch:
            aad = seq + bytes([kind]) + VERSION + struct.pack(">H", len(payload))
            c = AES.new(self.write_key, AES.MODE_GCM, nonce=self.write_salt + seq)
            c.update(aad)
            ct, tag = c.encrypt_and_digest(payload)
            payload = seq + ct + tag                 # the explicit nonce is the record's own epoch and number
        return bytes([kind]) + VERSION + seq + struct.pack(">H", len(payload)) + payload

    def _open(self, kind: int, seq: bytes, frag: bytes) -> bytes:
        if len(frag) < 24:
            raise DtlsError("short encrypted record")
        nonce, ct, tag = frag[:8], frag[8:-16], frag[-16:]
        aad = seq + bytes([kind]) + VERSION + struct.pack(">H", len(ct))
        c = AES.new(self.read_key, AES.MODE_GCM, nonce=self.read_salt + nonce)
        c.update(aad)
        try:
            return c.decrypt_and_verify(ct, tag)
        except ValueError as e:
            raise DtlsError("bad record from the server") from e

    @staticmethod
    def _records(datagram: bytes):
        i = 0
        while i + 13 <= len(datagram):
            kind, seq, n = datagram[i], datagram[i + 3:i + 11], struct.unpack(">H", datagram[i + 11:i + 13])[0]
            yield kind, seq, datagram[i + 13:i + 13 + n]
            i += 13 + n

    def send_datagram(self, data: bytes) -> None:
        self.sock.send(data)

    # -- handshake -----------------------------------------------------------
    def connect(self) -> None:
        self.sock.connect(self.addr)
        client_random = struct.pack(">I", int(time.time())) + os.urandom(28)

        def hello(cookie: bytes) -> bytes:
            return (VERSION + client_random + b"\x00" + bytes([len(cookie)]) + cookie
                    + struct.pack(">HH", 2, SUITE) + b"\x01\x00")

        cookie = b""
        transcript = b""
        got: dict = {}
        for _ in range(self.tries):
            msg = _handshake(CLIENT_HELLO, self.msg_seq, hello(cookie))
            self.send_datagram(self._record(HANDSHAKE, msg))
            got = self._read_flight(lambda g: HELLO_VERIFY_REQUEST in g or SERVER_HELLO_DONE in g)
            if HELLO_VERIFY_REQUEST in got:
                body = got[HELLO_VERIFY_REQUEST][1]
                cookie = body[3:3 + body[2]]
                self.msg_seq += 1                           # the hello with the cookie is a new message
                msg = _handshake(CLIENT_HELLO, self.msg_seq, hello(cookie))
                for _ in range(self.tries):
                    self.send_datagram(self._record(HANDSHAKE, msg))
                    got = self._read_flight(lambda g: SERVER_HELLO_DONE in g)
                    if SERVER_HELLO_DONE in got:
                        break
            if SERVER_HELLO_DONE in got:
                transcript = msg                            # (the hello and the verify request before it are left out)
                break
        if SERVER_HELLO_DONE not in got or SERVER_HELLO not in got:
            raise DtlsError("no answer from the server")
        for kind in (SERVER_HELLO, SERVER_KEY_EXCHANGE, SERVER_HELLO_DONE):
            if kind in got:
                transcript += got[kind][0]
        sh = got[SERVER_HELLO][1]
        server_random = sh[2:34]
        sid = sh[34]
        if struct.unpack(">H", sh[35 + sid:37 + sid])[0] != SUITE:
            raise DtlsError("the server chose another cipher suite")

        master = prf(psk_premaster(self.psk), b"master secret", client_random + server_random, 48)
        keys = prf(master, b"key expansion", server_random + client_random, 40)
        cw_key, sw_key, cw_iv, sw_iv = keys[:16], keys[16:32], keys[32:36], keys[36:40]

        self.msg_seq += 1
        cke = _handshake(CLIENT_KEY_EXCHANGE, self.msg_seq, struct.pack(">H", len(self.identity)) + self.identity)
        transcript += cke
        self.msg_seq += 1
        verify = prf(master, b"client finished", hashlib.sha256(transcript).digest(), 12)
        fin = _handshake(FINISHED, self.msg_seq, verify)
        server_verify = prf(master, b"server finished", hashlib.sha256(transcript + fin).digest(), 12)

        self.read_key, self.read_salt = sw_key, sw_iv
        self.write_key, self.write_salt = cw_key, cw_iv
        for _ in range(self.tries):                         # (a resent flight: the same messages, new record numbers)
            self.send_datagram(self._record(HANDSHAKE, cke, 0) + self._record(CHANGE_CIPHER_SPEC, b"\x01", 0)
                               + self._record(HANDSHAKE, fin, 1))
            done = self._read_finished()
            if done is not None:
                if not hmac.compare_digest(done, server_verify):
                    raise DtlsError("the server's Finished does not match: wrong key?")
                self.epoch, self.connected = 1, True
                return
        raise DtlsError("the handshake did not finish")

    def _read_flight(self, complete) -> dict:
        """Handshake messages from the server until complete(got) or a timeout: {type: (whole message, body)}."""
        got: dict = {}
        parts: dict = {}
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                data = self.sock.recv(4096)
            except (socket.timeout, ConnectionResetError):
                break
            for kind, _seq, frag in self._records(data):
                if kind == ALERT:
                    raise DtlsError(f"the server refused the handshake (alert {frag[1] if len(frag) > 1 else '?'})")
                if kind != HANDSHAKE:
                    continue
                i = 0
                while i + 12 <= len(frag):
                    t, n = frag[i], int.from_bytes(frag[i + 1:i + 4], "big")
                    mseq = struct.unpack(">H", frag[i + 4:i + 6])[0]
                    off, flen = int.from_bytes(frag[i + 6:i + 9], "big"), int.from_bytes(frag[i + 9:i + 12], "big")
                    buf = parts.setdefault((t, mseq), bytearray(n))
                    buf[off:off + flen] = frag[i + 12:i + 12 + flen]
                    have = parts.setdefault(("have", t, mseq), set())
                    have.update(range(off, off + flen))
                    if len(have) >= n:
                        got[t] = (_handshake(t, mseq, bytes(buf)), bytes(buf))
                    i += 12 + flen
            if complete(got):
                break
        return got

    def _read_finished(self) -> bytes | None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                data = self.sock.recv(4096)
            except (socket.timeout, ConnectionResetError):
                return None
            for kind, seq, frag in self._records(data):
                if kind == ALERT and seq[:2] == b"\x00\x00":
                    raise DtlsError(f"the server refused the handshake (alert {frag[1] if len(frag) > 1 else '?'})")
                if seq[:2] != b"\x00\x01":
                    continue                                # epoch 0: the server's resent flight, or its CCS
                plain = self._open(kind, seq, frag)
                if kind == ALERT:
                    raise DtlsError(f"the server refused the handshake (alert {plain[1] if len(plain) > 1 else '?'})")
                if kind == HANDSHAKE and plain[:1] == bytes([FINISHED]):
                    return plain[12:24]
        return None

    # -- data ----------------------------------------------------------------
    def send(self, data: bytes) -> None:
        if not self.connected:
            raise DtlsError("not connected")
        self.send_datagram(self._record(APPLICATION_DATA, data))

    def close(self) -> None:
        try:
            if self.connected:
                self.send_datagram(self._record(ALERT, b"\x01\x00"))       # close_notify
        except OSError:
            pass
        self.connected = False
        self.sock.close()
