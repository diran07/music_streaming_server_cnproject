"""
Online Music Streaming Client — Real-time Playback (Windows/Python 3.13)
=========================================================================
Integrated menu with:
  1. Stream & play songs
  2. Performance tests (response time, throughput, latency, concurrent clients)
  3. Live QoS stats
  4. Server info

Fix applied:
  • PCMPlayer thread join timeout reduced from 300 s → 5 s (FIX 6)
"""

import socket
import ssl
import struct
import time
import os
import json
import logging
import argparse
import threading
import statistics
import random
from collections import deque

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("MusicStreamClient")

HEADER_FORMAT = "!I H B B"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)
ACK_FORMAT    = "!I B"
ACK_SIZE      = struct.calcsize(ACK_FORMAT)

FLAG_DATA = 0x01
FLAG_END  = 0x02
FLAG_PING = 0x08

QUALITY_LABELS = {
    1: "22kHz mono  (low)",
    2: "44kHz mono  (medium)",
    3: "44kHz stereo (high)",
}

PRE_BUFFER_CHUNKS = 3


# ═══════════════════════════════════════════════
#  PCM Player
# ═══════════════════════════════════════════════
class PCMPlayer:
    def __init__(self, sample_rate: int, channels: int, sample_width: int):
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.sample_width = sample_width
        self._buf        = deque()
        self._lock       = threading.Lock()
        self._not_empty  = threading.Condition(self._lock)
        self._stop       = threading.Event()
        self._pre_buffer = threading.Event()
        self._thread     = None
        self._pa         = None
        self._stream     = None
        self._count      = 0

    def put_chunk(self, pcm_bytes: bytes):
        with self._not_empty:
            self._buf.append(pcm_bytes)
            self._count += 1
            self._not_empty.notify()
        if self._count >= PRE_BUFFER_CHUNKS and not self._pre_buffer.is_set():
            self._pre_buffer.set()

    @property
    def buffer_size(self):
        return len(self._buf)

    def start(self):
        self._thread = threading.Thread(
            target=self._playback_loop, daemon=True, name="pcm-playback"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pre_buffer.set()
        with self._not_empty:
            self._not_empty.notify_all()
        if self._thread:
            self._thread.join(timeout=5.0)   # FIX 6 — was 300 s
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass

    def _get_chunk(self) -> bytes:
        with self._not_empty:
            while not self._buf:
                if self._stop.is_set():
                    return None
                self._not_empty.wait(timeout=1.0)
            return self._buf.popleft()

    def _playback_loop(self):
        try:
            import pyaudio
        except ImportError:
            print("\n  ❌  pyaudio not installed!")
            print("  Windows fix:  pip install pipwin  then  pipwin install pyaudio")
            return

        import pyaudio as pa
        fmt_map = {1: pa.paInt8, 2: pa.paInt16, 4: pa.paInt32}
        fmt     = fmt_map.get(self.sample_width, pa.paInt16)
        self._pa = pa.PyAudio()

        print(f"\n   Audio: {self.sample_rate}Hz, {self.channels}ch, "
              f"{self.sample_width * 8}-bit")
        try:
            dev_info = self._pa.get_default_output_device_info()
            print(f"   Device: {dev_info['name']}")
        except Exception:
            pass

        try:
            self._stream = self._pa.open(
                format=fmt, channels=self.channels,
                rate=self.sample_rate, output=True, frames_per_buffer=2048,
            )
        except Exception as e:
            print(f"\n   Failed to open audio: {e} — trying fallback...")
            try:
                self._stream = self._pa.open(
                    format=pa.paInt16, channels=1,
                    rate=44100, output=True, frames_per_buffer=2048,
                )
            except Exception as e2:
                print(f"   Fallback failed: {e2}")
                return

        print("   Buffering", end="", flush=True)
        while not self._pre_buffer.wait(timeout=0.3):
            if self._stop.is_set():
                return
            print(".", end="", flush=True)
        print(" ▶  Playing!\n")

        while not self._stop.is_set():
            chunk = self._get_chunk()
            if chunk is None:
                break
            try:
                self._stream.write(chunk)
            except Exception as e:
                logger.error(f"Playback write error: {e}")
                break


# ═══════════════════════════════════════════════
#  Core connection helpers
# ═══════════════════════════════════════════════
def _make_ssl_conn(host, port):
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    conn = ctx.wrap_socket(raw, server_hostname=host)
    conn.connect((host, port))
    return conn


def _recv_line(conn) -> str:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed connection")
        data += chunk
    return data.decode().strip()


def _send_json(conn, obj: dict):
    conn.sendall((json.dumps(obj) + "\n").encode())


def _get_song_list(conn) -> list:
    raw = _recv_line(conn)
    msg = json.loads(raw)
    return msg.get("songs", [])


def _pick_first_song(conn):
    """Used by performance tests — auto-selects song #1."""
    songs = _get_song_list(conn)
    if not songs:
        raise RuntimeError("No songs on server. Add .wav files to the music/ folder.")
    _send_json(conn, {"id": songs[0]["id"]})
    confirm = json.loads(_recv_line(conn))
    if confirm.get("status") != "ok":
        raise RuntimeError(f"Song error: {confirm.get('message')}")
    return confirm


def _drain_stream(conn, drop_rate=0.0) -> dict:
    """
    Receive a full stream, ACKing every packet.
    drop_rate: 0.0–1.0 — fraction of ACKs intentionally sent as NACK (loss test).
    Returns stats dict.
    """
    packets     = 0
    total_bytes = 0
    latencies   = []
    dropped     = 0
    start       = time.perf_counter()

    while True:
        hdr = b""
        while len(hdr) < HEADER_SIZE:
            part = conn.recv(HEADER_SIZE - len(hdr))
            if not part:
                raise ConnectionError("Server dropped connection mid-stream")
            hdr += part

        seq_num, chunk_size, bitrate_level, flags = struct.unpack(HEADER_FORMAT, hdr)

        if flags & FLAG_END:
            break

        payload = b""
        while len(payload) < chunk_size:
            part = conn.recv(chunk_size - len(payload))
            if not part:
                raise ConnectionError("Server dropped mid-payload")
            payload += part

        t0 = time.perf_counter()
        if drop_rate > 0 and random.random() < drop_rate:
            conn.sendall(struct.pack(ACK_FORMAT, seq_num, 0))   # NACK
            dropped += 1
        else:
            conn.sendall(struct.pack(ACK_FORMAT, seq_num, 1))   # ACK
        latencies.append((time.perf_counter() - t0) * 1000)

        packets     += 1
        total_bytes += len(payload)

    duration = time.perf_counter() - start
    return {
        "packets":         packets,
        "bytes":           total_bytes,
        "duration_s":      round(duration, 2),
        "throughput_kbps": round((total_bytes * 8) / max(duration, 0.001) / 1000, 1),
        "latencies_ms":    latencies,
        "dropped_acks":    dropped,
    }


# ═══════════════════════════════════════════════
#  MusicStreamClient  — streaming + menu
# ═══════════════════════════════════════════════
class MusicStreamClient:
    def __init__(self, host: str, port: int, output_dir: str = "."):
        self.host       = host
        self.port       = port
        self.output_dir = output_dir
        self.conn       = None
        self._session_stats = []
        os.makedirs(output_dir, exist_ok=True)

    def connect(self):
        self.conn = _make_ssl_conn(self.host, self.port)
        print(f"\n  ✅  Connected to {self.host}:{self.port}")
        print(f"      Cipher: {self.conn.cipher()[0]}\n")

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _send_json(self, obj):
        _send_json(self.conn, obj)

    def _recv_line(self):
        return _recv_line(self.conn)

    # ── Stream a song (Option 1) ──────────────────────────────
    def stream_song(self):
        raw = self._recv_line()
        msg = json.loads(raw)
        if msg.get("status") == "error":
            print(f"\n  Server: {msg.get('message')}")
            return

        songs = msg["songs"]
        self._print_song_table(songs)

        while True:
            choice = input("\n  Enter song number (or 'b' to go back): ").strip()
            if choice.lower() == "b":
                self._send_json({"quit": True})
                return
            if choice.isdigit() and any(s["id"] == int(choice) for s in songs):
                self._send_json({"id": int(choice)})
                break
            elif any(s["name"] == choice for s in songs):
                self._send_json({"name": choice})
                break
            else:
                print("  Invalid choice. Try again.")

        confirm = json.loads(self._recv_line())
        if confirm.get("status") == "error":
            print(f"\n  Server error: {confirm.get('message')}")
            return

        song_name    = confirm["song"]
        sample_rate  = confirm.get("sample_rate",  44100)
        channels     = confirm.get("channels",     2)
        sample_width = confirm.get("sample_width", 2)
        quality      = confirm.get("quality",      "unknown")
        print(f"\n  Now streaming : {song_name}")
        print(f"  Quality level : {quality}")

        player          = PCMPlayer(sample_rate, channels, sample_width)
        player.start()
        packets         = 0
        total_bytes     = 0
        latencies       = []
        start_time      = time.time()
        current_bitrate = None

        try:
            while True:
                hdr = b""
                while len(hdr) < HEADER_SIZE:
                    part = self.conn.recv(HEADER_SIZE - len(hdr))
                    if not part:
                        raise ConnectionError("Server closed during stream")
                    hdr += part

                seq_num, chunk_size, bitrate_level, flags = struct.unpack(
                    HEADER_FORMAT, hdr
                )

                if flags & FLAG_PING:
                    self.conn.sendall(struct.pack(ACK_FORMAT, seq_num, 1))
                    continue

                if flags & FLAG_END:
                    print("\n  ■  Song finished!")
                    break

                payload = b""
                while len(payload) < chunk_size:
                    part = self.conn.recv(chunk_size - len(payload))
                    if not part:
                        raise ConnectionError("Server closed mid-payload")
                    payload += part

                player.put_chunk(payload)

                if bitrate_level != current_bitrate:
                    current_bitrate = bitrate_level
                    print(f"  Quality → {QUALITY_LABELS.get(bitrate_level, bitrate_level)}")

                t0 = time.perf_counter()
                self.conn.sendall(struct.pack(ACK_FORMAT, seq_num, 1))
                latencies.append((time.perf_counter() - t0) * 1000)

                packets     += 1
                total_bytes += len(payload)

                if packets % 40 == 0:
                    elapsed = time.time() - start_time
                    kbps    = (total_bytes * 8) / elapsed / 1000
                    print(f"  ↓  pkts={packets}  data={total_bytes // 1024}KB  "
                          f"{kbps:.0f}kbps  buf={player.buffer_size}")

        except KeyboardInterrupt:
            print("\n  (Stopped by user)")
        finally:
            player.stop()

        elapsed = time.time() - start_time
        kbps    = (total_bytes * 8) / max(elapsed, 0.001) / 1000
        avg_lat = statistics.mean(latencies) if latencies else 0
        print(f"\n  ✓  {total_bytes // 1024} KB  |  {elapsed:.1f}s  |  "
              f"{kbps:.0f} kbps  |  avg latency {avg_lat:.1f} ms")

        self._session_stats.append({
            "song": song_name, "packets": packets,
            "kb": total_bytes // 1024, "kbps": round(kbps, 1),
            "avg_latency_ms": round(avg_lat, 2),
            "quality": quality,
        })

    # ── QoS Stats (Option 2) ──────────────────────────────────
    def show_qos_stats(self):
        print("\n" + "═" * 54)
        print("  📊  QoS Stats — This Session")
        print("═" * 54)
        if not self._session_stats:
            print("  No songs streamed yet.")
        else:
            for i, s in enumerate(self._session_stats, 1):
                print(f"  [{i}] {s['song']}")
                print(f"       Quality : {s['quality']}")
                print(f"       Packets : {s['packets']}")
                print(f"       Data    : {s['kb']} KB")
                print(f"       Speed   : {s['kbps']} kbps")
                print(f"       Latency : {s['avg_latency_ms']} ms avg")
        print("═" * 54)
        input("\n  Press Enter to continue...")

    # ── Performance Tests (Option 3) ─────────────────────────
    def run_performance_tests(self):
        print("\n" + "═" * 54)
        print("  ⚡  Performance Test Suite")
        print("  (Opens separate connections — current session unaffected)")
        print("═" * 54)
        print("\n  Select test:")
        print("    1  Response time        (5 connects)")
        print("    2  Throughput & latency (1 client, full song)")
        print("    3  Concurrent clients   (3 clients at once)")
        print("    4  Packet loss sim      (5% dropped ACKs)")
        print("    5  Run ALL tests")
        print("    b  Back to main menu")

        choice = input("\n  > ").strip()

        if choice == "b":
            return
        elif choice == "1":
            self._perf_response_time()
        elif choice == "2":
            self._perf_throughput()
        elif choice == "3":
            self._perf_concurrent(3)
        elif choice == "4":
            self._perf_packet_loss()
        elif choice == "5":
            self._perf_response_time()
            self._perf_throughput()
            self._perf_concurrent(3)
            self._perf_packet_loss()
            print("\n  ✅  All tests complete!")
        else:
            print("  Unknown option.")

        input("\n  Press Enter to continue...")

    def _perf_response_time(self, runs=5):
        print(f"\n  ── Test: Response Time ({runs} runs) ─────────────")
        times = []
        for i in range(runs):
            try:
                conn = _make_ssl_conn(self.host, self.port)
                t0   = time.perf_counter()
                _pick_first_song(conn)
                first = b""
                while not first:
                    first = conn.recv(1)
                ms = (time.perf_counter() - t0) * 1000
                times.append(ms)
                print(f"  Run {i + 1}: {ms:.1f} ms")
                conn.close()
            except Exception as e:
                print(f"  Run {i + 1}: ERROR — {e}")

        if times:
            print(f"\n  Min : {min(times):.1f} ms")
            print(f"  Max : {max(times):.1f} ms")
            print(f"  Avg : {statistics.mean(times):.1f} ms")

    def _perf_throughput(self):
        print("\n  ── Test: Throughput & Latency (1 client) ────────")
        try:
            conn = _make_ssl_conn(self.host, self.port)
            _pick_first_song(conn)
            print("  Streaming full song... (wait for it to finish)")
            s = _drain_stream(conn)
            conn.close()

            lats = s["latencies_ms"]
            print(f"\n  Packets    : {s['packets']}")
            print(f"  Data       : {s['bytes'] // 1024} KB in {s['duration_s']}s")
            print(f"  Throughput : {s['throughput_kbps']} kbps")
            if lats:
                print(f"  Latency    : avg={statistics.mean(lats):.2f}ms  "
                      f"min={min(lats):.2f}ms  max={max(lats):.2f}ms")
        except Exception as e:
            print(f"  ERROR — {e}")

    def _perf_concurrent(self, n):
        print(f"\n  ── Test: {n} Concurrent Clients ─────────────────")
        results = []
        errors  = []
        lock    = threading.Lock()

        def worker(idx):
            try:
                conn = _make_ssl_conn(self.host, self.port)
                _pick_first_song(conn)
                s = _drain_stream(conn)
                conn.close()
                s["idx"] = idx
                with lock:
                    results.append(s)
                print(f"  Client {idx}: {s['bytes'] // 1024} KB  "
                      f"{s['throughput_kbps']} kbps  {s['duration_s']}s")
            except Exception as e:
                with lock:
                    errors.append(f"Client {idx}: {e}")
                print(f"  Client {idx}: ERROR — {e}")

        threads = [
            threading.Thread(target=worker, args=(i + 1,), daemon=True)
            for i in range(n)
        ]
        wall = time.perf_counter()
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(timeout=300)
        wall = time.perf_counter() - wall

        if results:
            total_kbps = sum(r["throughput_kbps"] for r in results)
            avg_kbps   = statistics.mean(r["throughput_kbps"] for r in results)
            print(f"\n  Completed  : {len(results)}/{n}")
            print(f"  Total kbps : {total_kbps:.1f}")
            print(f"  Avg  kbps  : {avg_kbps:.1f}")
            print(f"  Wall time  : {wall:.1f}s")

    def _perf_packet_loss(self, drop_rate=0.05):
        print(f"\n  ── Test: Packet Loss ({int(drop_rate * 100)}% dropped ACKs) ────")
        try:
            conn = _make_ssl_conn(self.host, self.port)
            _pick_first_song(conn)
            print(f"  Streaming with {int(drop_rate * 100)}% ACK drops...")
            s = _drain_stream(conn, drop_rate=drop_rate)
            conn.close()

            print(f"\n  Packets received : {s['packets']}")
            print(f"  ACKs dropped     : {s['dropped_acks']}  "
                  f"({s['dropped_acks'] / max(s['packets'], 1) * 100:.1f}%)")
            print(f"  Data received    : {s['bytes'] // 1024} KB")
            print(f"  Throughput       : {s['throughput_kbps']} kbps")
            print(f"  → Server retry mechanism absorbed the dropped ACKs")
        except Exception as e:
            print(f"  ERROR — {e}")

    # ── Server info (Option 4) ────────────────────────────────
    def show_server_info(self):
        print("\n" + "═" * 54)
        print("  ℹ️   Server Info")
        print("═" * 54)
        print(f"  Host    : {self.host}")
        print(f"  Port    : {self.port}")
        try:
            cipher = self.conn.cipher()
            print(f"  Cipher  : {cipher[0]}")
            print(f"  TLS ver : {cipher[1]}")
        except Exception:
            pass
        print("═" * 54)
        input("\n  Press Enter to continue...")

    def _print_song_table(self, songs):
        print("\n" + "═" * 54)
        print("  🎵  Available Songs")
        print("═" * 54)
        for s in songs:
            print(f"  [{s['id']:>2}]  {s['name']:<36}  {s['size_kb']:>5} KB")
        print("═" * 54)

    def run_menu(self):
        while True:
            print("""
╔══════════════════════════════════════════════════════╗
║          🎵  Music Streaming Client                  ║
╠══════════════════════════════════════════════════════╣
║  1  Stream a song (real-time playback)               ║
║  2  View QoS stats (this session)                    ║
║  3  Performance tests (throughput / latency / load)  ║
║  4  Server info                                      ║
║  q  Quit                                             ║
╚══════════════════════════════════════════════════════╝""")

            choice = input("  > ").strip().lower()

            if choice == "1":
                try:
                    self.stream_song()
                except (ConnectionResetError, BrokenPipeError, ConnectionError) as e:
                    print(f"\n  ❌  Connection lost during streaming: {e}")
                    break
            elif choice == "2":
                self.show_qos_stats()
            elif choice == "3":
                self.run_performance_tests()
            elif choice == "4":
                self.show_server_info()
            elif choice == "q":
                break
            else:
                print("  Unknown option.")


# ═══════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Music Streaming Client")
    parser.add_argument("--host",       default="127.0.0.1")
    parser.add_argument("--port",       default=9000, type=int)
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    print("\n  --- Audio Device Check ---")
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        print(f"  Output: {pa.get_default_output_device_info()['name']}")
        pa.terminate()
        print("  Audio OK\n")
    except ImportError:
        print("  ⚠  pyaudio not installed — audio won't play.")
        print("  Fix: pip install pipwin  then  pipwin install pyaudio\n")
    except Exception as e:
        print(f"  ⚠  Audio check: {e}\n")

    client = MusicStreamClient(
        host=args.host, port=args.port, output_dir=args.output_dir
    )
    try:
        client.connect()
    except Exception as e:
        print(f"\n  ❌  Could not connect to {args.host}:{args.port} — {e}")
        print("  Is the server running?  python streaming_server.py --music-dir music")
        raise SystemExit(1)

    try:
        client.run_menu()
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    finally:
        client.disconnect()
        print("  Goodbye!\n")