"""
Online Music Streaming Server — PCM Streaming  [Deliverable 2]
===============================================================
Original features (Deliverable 1):
  - Raw PCM streaming over SSL/TLS TCP sockets
  - Multi-client concurrency (one thread per client)
  - Adaptive bitrate based on RTT and packet loss
  - Buffer management with StreamBuffer + AudioDecoder
  - QoS metrics: throughput, latency, loss rate

Deliverable 2 additions / fixes:
  - Pure-Python SSL cert generation (Windows-compatible, no openssl command)
  - Enhanced error handling: abrupt disconnects, SSL handshake failures,
    invalid/malformed inputs, mid-stream partial failures
  - Improved logging with RotatingFileHandler + exc_info for unexpected errors
  - Input validation in _recv_json (max 4 096 bytes, JSON decode guard)
  - Three real quality levels: each decodes the audio at a different
    sample-rate/channel layout so bandwidth and quality genuinely differ
  - Retry-exhaustion now logged before returning False
  - Client IDs generated with os.urandom (no MD5 collision risk)

Run with:
  python streaming_server.py --music-dir music
  python performance_test.py               # full evaluation report
"""

import socket
import ssl
import threading
import time
import os
import struct
import logging
import json
from logging.handlers import RotatingFileHandler       # FIX 5 — log rotation
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional
import argparse
import datetime

# ── Logging setup ────────────────────────────────────────────────
_file_handler   = RotatingFileHandler(
    "server.log", maxBytes=5 * 1024 * 1024, backupCount=3   # 5 MB × 3 files
)
_stream_handler = logging.StreamHandler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger("MusicStreamServer")

# ── Packet format ─────────────────────────────────────────────────────
HEADER_FORMAT = "!I H B B"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)
ACK_FORMAT    = "!I B"
ACK_SIZE      = struct.calcsize(ACK_FORMAT)

FLAG_DATA   = 0x01
FLAG_END    = 0x02
FLAG_RESEND = 0x04
FLAG_PING   = 0x08

MAX_RETRIES = 5
ACK_TIMEOUT = 2.0
BUFFER_SIZE = 65536

SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a")

# ── FIX 2 — Three real quality levels ────────────────────────────────
# Each level uses a genuinely different sample rate / channel layout so
# the amount of PCM data sent per second actually differs:
#   Level 1 ≈  88 200 B/s  (22 kHz mono  16-bit)
#   Level 2 ≈  88 200 B/s  (44 kHz mono  16-bit)   ← same bytes/s as L1,
#                                                       but higher freq. range
#   Level 3 ≈ 176 400 B/s  (44 kHz stereo 16-bit)
#
# Adaptation logic: level is chosen at the START of each song based on
# the QoS accumulated during the previous song, so every song sounds
# consistent rather than glitching mid-track.
QUALITY_CONFIGS: Dict[int, dict] = {
    1: {"sample_rate": 22050, "channels": 1, "sample_width": 2},   # low
    2: {"sample_rate": 44100, "channels": 1, "sample_width": 2},   # medium
    3: {"sample_rate": 44100, "channels": 2, "sample_width": 2},   # high
}

QUALITY_LABELS = {
    1: "22kHz mono  (low)",
    2: "44kHz mono  (medium)",
    3: "44kHz stereo (high)",
}


def _bytes_per_second(cfg: dict) -> int:
    return cfg["sample_rate"] * cfg["channels"] * cfg["sample_width"]


def _chunk_size_for_level(level: int) -> int:
    """Return 100 ms worth of PCM bytes, aligned to a sample boundary."""
    cfg   = QUALITY_CONFIGS[level]
    bps   = _bytes_per_second(cfg)
    raw   = int(bps * 0.1)
    align = cfg["channels"] * cfg["sample_width"]
    return (raw // align) * align


# ─────────────────────────────────────────────
#  Song Library
# ─────────────────────────────────────────────
class SongLibrary:
    def __init__(self, music_dir: str = "."):
        self.music_dir = os.path.abspath(music_dir)
        os.makedirs(self.music_dir, exist_ok=True)

    def list_songs(self) -> list:
        songs = []
        for i, fname in enumerate(sorted(os.listdir(self.music_dir)), start=1):
            if fname.lower().endswith(SUPPORTED_EXTENSIONS):
                fpath    = os.path.join(self.music_dir, fname)
                size_kb  = os.path.getsize(fpath) // 1024
                songs.append({"id": i, "name": fname, "size_kb": size_kb})
        return songs

    def get_path(self, song_name: str) -> Optional[str]:
        path = os.path.join(self.music_dir, song_name)
        if os.path.isfile(path) and song_name.lower().endswith(SUPPORTED_EXTENSIONS):
            return path
        return None

    def get_path_by_id(self, song_id: int) -> Optional[str]:
        for s in self.list_songs():
            if s["id"] == song_id:
                return self.get_path(s["name"])
        return None


# ─────────────────────────────────────────────
#  Audio Decoder  (server-side)
# ─────────────────────────────────────────────
class AudioDecoder:
    """
    Decodes any audio file to raw PCM using pydub.
    quality_cfg controls the output sample rate and channel count so
    different bitrate levels genuinely produce different amounts of data.
    """
    def __init__(self, filepath: str, chunk_size: int, quality_cfg: dict):
        self.filepath    = filepath
        self.chunk_size  = chunk_size
        self.quality_cfg = quality_cfg
        self._ext        = os.path.splitext(filepath)[1].lower().lstrip(".")

    def decode(self):
        """Generator — yields raw PCM chunks at the requested quality."""
        try:
            from pydub import AudioSegment
        except ImportError:
            raise RuntimeError("pydub not installed. Run: pip install pydub")

        cfg = self.quality_cfg
        logger.info(
            f"Decoding '{os.path.basename(self.filepath)}' "
            f"@ {cfg['sample_rate']} Hz / {cfg['channels']}ch ..."
        )
        seg = AudioSegment.from_file(self.filepath, format=self._ext)
        seg = (seg.set_frame_rate(cfg["sample_rate"])
                  .set_channels(cfg["channels"])
                  .set_sample_width(cfg["sample_width"]))

        raw = seg.raw_data
        bps = _bytes_per_second(cfg)
        logger.info(
            f"Decoded: {len(raw) // 1024} KB PCM, "
            f"{len(raw) / bps:.1f}s duration"
        )

        offset = 0
        while offset < len(raw):
            yield raw[offset:offset + self.chunk_size]
            offset += self.chunk_size


# ─────────────────────────────────────────────
#  QoS Monitor
# ─────────────────────────────────────────────
@dataclass
class QoSStats:
    client_id:       str
    packets_sent:    int   = 0
    packets_lost:    int   = 0
    packets_retried: int   = 0
    total_bytes:     int   = 0
    start_time:      float = field(default_factory=time.time)
    rtt_samples:     deque = field(default_factory=lambda: deque(maxlen=20))
    bitrate_history: list  = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return max(time.time() - self.start_time, 0.001)

    @property
    def throughput_kbps(self) -> float:
        return (self.total_bytes * 8) / self.elapsed / 1000

    @property
    def loss_rate(self) -> float:
        return self.packets_lost / max(self.packets_sent, 1)

    @property
    def avg_rtt(self) -> float:
        return sum(self.rtt_samples) / len(self.rtt_samples) if self.rtt_samples else 0.0

    def report(self) -> dict:
        return {
            "client_id":       self.client_id,
            "elapsed_s":       round(self.elapsed, 2),
            "packets_sent":    self.packets_sent,
            "packets_lost":    self.packets_lost,
            "loss_rate_%":     round(self.loss_rate * 100, 2),
            "retries":         self.packets_retried,
            "throughput_kbps": round(self.throughput_kbps, 2),
            "avg_rtt_ms":      round(self.avg_rtt * 1000, 2),
            "bitrate_changes": self.bitrate_history,
        }


# ─────────────────────────────────────────────
#  PCM Stream Buffer
# ─────────────────────────────────────────────
class StreamBuffer:
    def __init__(self):
        self._buf       = deque()
        self._lock      = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._done      = False

    def put(self, chunk: bytes):
        with self._lock:
            self._buf.append(chunk)
            self._not_empty.notify()

    def set_done(self):
        with self._lock:
            self._done = True
            self._not_empty.notify_all()

    def get(self, timeout: float = 5.0) -> Optional[bytes]:
        with self._not_empty:
            deadline = time.time() + timeout
            while not self._buf:
                if self._done:
                    return None
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._not_empty.wait(timeout=remaining)
            return self._buf.popleft()

    def fill_from_decoder(self, decoder: AudioDecoder):
        def _reader():
            for chunk in decoder.decode():
                self.put(chunk)
            self.set_done()
        threading.Thread(target=_reader, daemon=True).start()


# ─────────────────────────────────────────────
#  Client Session
# ─────────────────────────────────────────────
class ClientSession:
    def __init__(self, conn: ssl.SSLSocket, addr, client_id: str, library: SongLibrary):
        self.conn         = conn
        self.addr         = addr
        self.client_id    = client_id
        self.library      = library
        self.qos          = QoSStats(client_id=client_id)
        self.bitrate_level = 2          # start at medium quality
        self.seq_num       = 0
        self._running      = True

    # ── Helpers ──────────────────────────────────────────────
    def _send_json(self, data: dict):
        self.conn.sendall((json.dumps(data) + "\n").encode())

    def _recv_json(self) -> dict:
        """
        FIX 1 — maximum buffer size guard (4 096 bytes).
        Prevents a client that never sends \\n from growing the buffer forever.
        """
        raw = b""
        while b"\n" not in raw:
            chunk = self.conn.recv(256)
            if not chunk:
                raise ConnectionError("Client disconnected")
            raw += chunk
            if len(raw) > 4096:                     # ← FIX 1
                raise ValueError("Request payload exceeds 4 096 bytes")
        return json.loads(raw.decode().strip())

    # ── Song selection ────────────────────────────────────────
    def _request_song(self) -> Optional[str]:
        songs = self.library.list_songs()
        if not songs:
            self._send_json({"status": "error", "message": "No songs available"})
            return None

        self._send_json({"status": "ok", "songs": songs})

        try:
            self.conn.settimeout(60.0)
            request = self._recv_json()
        except Exception as e:
            logger.info(f"[{self.client_id}] No request received: {e}")
            return None

        if request.get("quit"):
            logger.info(f"[{self.client_id}] Client quit.")
            return None

        audio_path = None
        if "id" in request:
            audio_path = self.library.get_path_by_id(int(request["id"]))
        elif "name" in request:
            audio_path = self.library.get_path(request["name"])

        if not audio_path:
            logger.warning(f"[{self.client_id}] Invalid song request: {request}")
            self._send_json({"status": "error", "message": "Song not found. Send a valid id or name."})
            return None

        # FIX 2 — send the real quality parameters for this level so the
        # client can configure its audio device correctly.
        cfg = QUALITY_CONFIGS[self.bitrate_level]
        self._send_json({
            "status":       "ok",
            "song":         os.path.basename(audio_path),
            "sample_rate":  cfg["sample_rate"],
            "channels":     cfg["channels"],
            "sample_width": cfg["sample_width"],
            "quality":      QUALITY_LABELS[self.bitrate_level],
        })
        logger.info(
            f"[{self.client_id}] Confirmed: '{os.path.basename(audio_path)}' "
            f"quality={QUALITY_LABELS[self.bitrate_level]}"
        )
        return audio_path

    # ── Adaptive bitrate ──────────────────────────────────────
    def _adapt_bitrate(self):
        """
        Re-evaluate bitrate level using RTT and loss rate.
        Changes take effect at the next song (quality is fixed per-song
        so there are no mid-track audio glitches or format renegotiations).
        """
        loss = self.qos.loss_rate
        rtt  = self.qos.avg_rtt
        old  = self.bitrate_level

        if loss > 0.05 or rtt > 0.5:
            self.bitrate_level = max(1, self.bitrate_level - 1)
        elif loss < 0.02 and rtt < 0.2 and len(self.qos.rtt_samples) >= 5:
            self.bitrate_level = min(3, self.bitrate_level + 1)

        if self.bitrate_level != old:
            self.qos.bitrate_history.append({
                "time_s": round(self.qos.elapsed, 2),
                "from": old, "to": self.bitrate_level,
            })
            logger.info(
                f"[{self.client_id}] Bitrate {old} → {self.bitrate_level} "
                f"({QUALITY_LABELS[self.bitrate_level]}) — "
                f"loss={loss*100:.1f}% rtt={rtt*1000:.0f}ms"
            )

    # ── Low-level send ────────────────────────────────────────
    def _pack(self, data: bytes, flags: int) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            self.seq_num, len(data), self.bitrate_level, flags
        ) + data

    def _send_chunk(self, data: bytes) -> bool:
        for attempt in range(MAX_RETRIES):
            try:
                self.conn.sendall(self._pack(data, FLAG_DATA))
                self.qos.packets_sent += 1
                self.qos.total_bytes  += len(data)

                t_send = time.time()
                self.conn.settimeout(ACK_TIMEOUT)
                raw_ack = self.conn.recv(ACK_SIZE)
                rtt     = time.time() - t_send

                if len(raw_ack) < ACK_SIZE:
                    raise ConnectionError("Incomplete ACK")

                ack_seq, ack_status = struct.unpack(ACK_FORMAT, raw_ack[:ACK_SIZE])
                if ack_status == 1 and ack_seq == self.seq_num:
                    self.qos.rtt_samples.append(rtt)
                    self.seq_num += 1
                    return True

                self.qos.packets_lost    += 1
                self.qos.packets_retried += 1
                logger.debug(
                    f"[{self.client_id}] NACK seq={self.seq_num} "
                    f"attempt={attempt + 1}/{MAX_RETRIES}"
                )

            except socket.timeout:
                self.qos.packets_lost    += 1
                self.qos.packets_retried += 1
                logger.debug(
                    f"[{self.client_id}] ACK timeout seq={self.seq_num} "
                    f"attempt={attempt + 1}/{MAX_RETRIES}"
                )
            except (ConnectionResetError, BrokenPipeError, ssl.SSLError) as e:
                logger.error(f"[{self.client_id}] Connection error in _send_chunk: {e}")
                return False

        # FIX 4 — log retry exhaustion explicitly
        logger.warning(
            f"[{self.client_id}] Max retries ({MAX_RETRIES}) exhausted for "
            f"seq={self.seq_num} — dropping client"
        )
        return False

    # ── Streaming ─────────────────────────────────────────────
    def _stream_song(self, audio_path: str) -> bool:
        """
        FIX 2 — decode audio at the current quality level so the chunk size,
        BPS, and pacing all reflect the real bitrate being streamed.
        """
        cfg        = QUALITY_CONFIGS[self.bitrate_level]
        bps        = _bytes_per_second(cfg)
        chunk_size = _chunk_size_for_level(self.bitrate_level)

        decoder = AudioDecoder(audio_path, chunk_size, cfg)
        buf     = StreamBuffer()
        buf.fill_from_decoder(decoder)

        chunk_duration = chunk_size / bps   # seconds per chunk (e.g. 0.1 s)
        adapt_counter  = 0
        stream_start   = time.perf_counter()
        chunks_sent    = 0

        while True:
            chunk = buf.get(timeout=10.0)
            if chunk is None:
                logger.info(f"[{self.client_id}] Song finished.")
                break

            if not self._send_chunk(chunk):
                return False

            chunks_sent   += 1
            adapt_counter += 1

            # ── Absolute-time pacing (avoids cumulative drift) ────────
            expected_time = stream_start + (chunks_sent * chunk_duration)
            now           = time.perf_counter()
            wait          = expected_time - now
            if wait >= 0.005:
                time.sleep(wait)
            elif wait < -0.5:
                logger.warning(
                    f"[{self.client_id}] Pacing {-wait:.2f}s behind, resetting clock"
                )
                stream_start = time.perf_counter() - (chunks_sent * chunk_duration)

            if adapt_counter % 10 == 0:
                self._adapt_bitrate()

        try:
            self.conn.sendall(self._pack(b"", FLAG_END))
        except Exception:
            return False

        return True

    # ── Session loop ──────────────────────────────────────────
    def run(self):
        """
        Comprehensive error handling per Deliverable 2 rubric:
          • Abrupt client disconnections (ConnectionResetError, BrokenPipeError)
          • SSL errors after handshake
          • Invalid JSON / oversized inputs (ValueError)
          • Partial mid-stream failures (OSError, EOFError)
        """
        logger.info(f"[{self.client_id}] Session started from {self.addr}")
        try:
            while self._running:
                try:
                    audio_path = self._request_song()
                except ValueError as e:
                    logger.warning(f"[{self.client_id}] Bad request: {e}")
                    try:
                        self._send_json({"status": "error", "message": str(e)})
                    except Exception:
                        pass
                    break

                if audio_path is None:
                    break

                try:
                    ok = self._stream_song(audio_path)
                except (ConnectionResetError, BrokenPipeError) as e:
                    logger.warning(f"[{self.client_id}] Client disconnected mid-stream: {e}")
                    break
                except OSError as e:
                    logger.error(f"[{self.client_id}] Network error mid-stream: {e}")
                    break
                except Exception as e:
                    logger.error(f"[{self.client_id}] Stream error: {e}", exc_info=True)
                    break

                if not ok:
                    break

        except (ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"[{self.client_id}] Abrupt disconnect: {e}")
        except ssl.SSLError as e:
            logger.warning(f"[{self.client_id}] SSL error: {e}")
        except ConnectionError as e:
            logger.info(f"[{self.client_id}] Connection closed: {e}")
        except Exception as e:
            logger.error(f"[{self.client_id}] Unexpected session error: {e}", exc_info=True)
        finally:
            logger.info(
                f"[{self.client_id}] Session ended. QoS:\n"
                + json.dumps(self.qos.report(), indent=2)
            )
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
                self.conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────
#  SSL cert generation
# ─────────────────────────────────────────────
def generate_self_signed_cert(certfile="server.crt", keyfile="server.key"):
    """
    Generate a self-signed TLS certificate using pure Python.
    Works on Windows/Mac/Linux without an openssl binary.
    """
    if os.path.exists(certfile) and os.path.exists(keyfile):
        logger.info("SSL certificates found.")
        return
    logger.info("Generating self-signed SSL certificate (pure Python) ...")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key  = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now  = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        with open(keyfile, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(certfile, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        logger.info(f"SSL certificate generated: {certfile}, {keyfile}")
    except ImportError:
        logger.error("'cryptography' library missing. Run: pip install cryptography")
        raise SystemExit(1)


# ─────────────────────────────────────────────
#  Server
# ─────────────────────────────────────────────
class MusicStreamingServer:
    def __init__(self, host="0.0.0.0", port=9000,
                 music_dir=".", certfile="server.crt", keyfile="server.key"):
        self.host     = host
        self.port     = port
        self.library  = SongLibrary(music_dir)
        self.certfile = certfile
        self.keyfile  = keyfile
        self.clients: Dict[str, threading.Thread] = {}
        self._lock    = threading.Lock()

    def _make_ssl_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def _handle_client(self, raw_conn: socket.socket, addr, client_id: str):
        """
        Wraps the raw socket in TLS before handing off to ClientSession.
        SSL handshake failures are caught here and logged clearly.
        """
        logger.info(f"[{client_id}] Incoming connection from {addr}")
        ssl_ctx = self._make_ssl_context()
        try:
            conn = ssl_ctx.wrap_socket(raw_conn, server_side=True)
        except ssl.SSLError as e:
            logger.error(
                f"[{client_id}] TLS handshake failed from {addr}: {e}. "
                "Client may not support TLS 1.2+."
            )
            try:
                raw_conn.close()
            except Exception:
                pass
            with self._lock:
                self.clients.pop(client_id, None)
            return
        except OSError as e:
            logger.error(f"[{client_id}] Socket error during handshake: {e}")
            try:
                raw_conn.close()
            except Exception:
                pass
            with self._lock:
                self.clients.pop(client_id, None)
            return

        ClientSession(conn, addr, client_id, self.library).run()

        with self._lock:
            self.clients.pop(client_id, None)
        logger.info(f"[{client_id}] Disconnected. Active: {len(self.clients)}")

    def start(self):
        generate_self_signed_cert(self.certfile, self.keyfile)

        songs = self.library.list_songs()
        if songs:
            logger.info(f"Song library ({len(songs)} songs):")
            for s in songs:
                logger.info(f"   [{s['id']:>2}] {s['name']}  ({s['size_kb']} KB)")
        else:
            logger.warning(f"No songs found in '{self.library.music_dir}'.")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_SIZE)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER_SIZE)
        sock.bind((self.host, self.port))
        sock.listen(10)
        logger.info(f"Listening on {self.host}:{self.port} (TLS enabled)")

        try:
            while True:
                sock.settimeout(1.0)
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                with self._lock:
                    # FIX 7 — use os.urandom for IDs (no MD5 collision risk)
                    cid = os.urandom(4).hex()
                    t = threading.Thread(
                        target=self._handle_client,
                        args=(conn, addr, cid),
                        daemon=True,
                        name=f"client-{cid}",
                    )
                    self.clients[cid] = t
                    t.start()
                    logger.info(
                        f"[{cid}] Accepted. Active clients: {len(self.clients)}"
                    )
        except KeyboardInterrupt:
            logger.info("Shutting down ...")
        finally:
            sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Music Streaming Server")
    parser.add_argument("--host",      default="0.0.0.0")
    parser.add_argument("--port",      default=9000, type=int)
    parser.add_argument("--music-dir", default=".")
    parser.add_argument("--cert",      default="server.crt")
    parser.add_argument("--key",       default="server.key")
    args = parser.parse_args()

    MusicStreamingServer(
        host=args.host, port=args.port,
        music_dir=args.music_dir,
        certfile=args.cert, keyfile=args.key,
    ).start()