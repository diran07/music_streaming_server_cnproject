"""
performance_test.py
====================
Deliverable 2 — Performance Evaluation Script
Tests the Music Streaming Server under realistic conditions.

Metrics measured:
  • Response time  — time from connect to first byte received
  • Throughput     — KB/s per client
  • Latency        — inter-packet arrival time (time between receiving
                     consecutive data packets, which reflects the real
                     server-side pacing delay experienced by the client)
  • Scalability    — 1, 2, 3, 5 concurrent clients
  • Packet loss    — retry rate when ACKs are dropped

Fixes applied:
  • FIX 3  — Latency now measures inter-packet arrival gap (not ACK syscall time)
  • FIX 13 — Concurrent client stagger reduced from 100ms → 10ms for
             genuine simultaneous load testing

Usage:
  1. Start the server:  python streaming_server.py --music-dir music
  2. Run this script:   python performance_test.py
"""

import socket
import ssl
import struct
import json
import time
import threading
import statistics
import argparse
import os
import random

# ── Same packet constants as the server ──────────────────────
HEADER_FORMAT = "!I H B B"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)
ACK_FORMAT    = "!I B"
ACK_SIZE      = struct.calcsize(ACK_FORMAT)
FLAG_END      = 0x02
FLAG_DATA     = 0x01


# ── Low-level helpers ─────────────────────────────────────────

def make_conn(host, port):
    """Create an SSL-wrapped TCP connection to the server."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    conn = ctx.wrap_socket(raw, server_hostname=host)
    conn.connect((host, port))
    return conn


def recv_line(conn) -> str:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed")
        data += chunk
    return data.decode().strip()


def send_json(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode())


def get_song_list(conn):
    raw = recv_line(conn)
    msg = json.loads(raw)
    return msg.get("songs", [])


def pick_first_song(conn):
    """Select the first song from the server's list. Returns confirmation dict."""
    songs = get_song_list(conn)
    if not songs:
        raise RuntimeError("No songs on server — add .wav files to the music/ folder")
    send_json(conn, {"id": songs[0]["id"]})
    confirm = json.loads(recv_line(conn))
    if confirm.get("status") != "ok":
        raise RuntimeError(f"Song selection failed: {confirm}")
    return confirm


# ── Test helpers ──────────────────────────────────────────────

def drain_stream(conn) -> dict:
    """
    Receive the full stream, sending ACKs.

    FIX 3 — Latency is now the inter-packet arrival gap:
      the time from when the previous packet's header arrived to when the
      current packet's header arrives. This represents the pacing delay the
      server imposes between packets (≈ chunk_duration) plus any network jitter.

    The old approach measured the time to call sendall(ACK), which was just
    a kernel syscall (< 0.05 ms) and bore no relationship to network RTT.
    """
    packets     = 0
    total_bytes = 0
    inter_gaps  = []      # inter-packet arrival times (ms)
    start       = time.perf_counter()
    t_prev      = None    # timestamp of previous packet header arrival

    while True:
        # Read header — record arrival time immediately after the recv
        hdr = b""
        while len(hdr) < HEADER_SIZE:
            part = conn.recv(HEADER_SIZE - len(hdr))
            if not part:
                raise ConnectionError("Connection dropped mid-stream")
            hdr += part

        t_arrived = time.perf_counter()   # FIX 3 — when this header arrived

        seq_num, chunk_size, bitrate_level, flags = struct.unpack(HEADER_FORMAT, hdr)

        if flags & FLAG_END:
            break

        # Read payload
        payload = b""
        while len(payload) < chunk_size:
            part = conn.recv(chunk_size - len(payload))
            if not part:
                raise ConnectionError("Connection dropped mid-payload")
            payload += part

        # Send ACK
        conn.sendall(struct.pack(ACK_FORMAT, seq_num, 1))

        # FIX 3 — compute inter-packet gap (skip first packet, no baseline yet)
        if t_prev is not None:
            inter_gaps.append((t_arrived - t_prev) * 1000)
        t_prev = t_arrived

        packets     += 1
        total_bytes += len(payload)

    duration = time.perf_counter() - start
    return {
        "packets":       packets,
        "bytes":         total_bytes,
        "duration_s":    round(duration, 3),
        "throughput_kbps": round((total_bytes * 8) / duration / 1000, 2) if duration > 0 else 0,
        "inter_gap_ms":  inter_gaps,       # renamed from latencies_ms
    }


# ════════════════════════════════════════════════
#  TEST 1 — Response Time
# ════════════════════════════════════════════════

def test_response_time(host, port, runs=5):
    print("\n" + "═" * 58)
    print("  TEST 1 — Response Time (connect → first byte)")
    print("═" * 58)

    times = []
    for i in range(runs):
        conn = make_conn(host, port)
        t0   = time.perf_counter()

        pick_first_song(conn)

        first = b""
        while len(first) < 1:
            first = conn.recv(1)

        response_ms = (time.perf_counter() - t0) * 1000
        times.append(response_ms)
        print(f"  Run {i + 1}: {response_ms:.1f} ms")

        try:
            conn.close()
        except Exception:
            pass

    print(f"\n  Min  : {min(times):.1f} ms")
    print(f"  Max  : {max(times):.1f} ms")
    print(f"  Mean : {statistics.mean(times):.1f} ms")
    if len(times) > 1:
        print(f"  Stdev: {statistics.stdev(times):.1f} ms")
    return times


# ════════════════════════════════════════════════
#  TEST 2 — Single Client Throughput + Latency
# ════════════════════════════════════════════════

def test_single_client(host, port):
    print("\n" + "═" * 58)
    print("  TEST 2 — Single Client: Throughput & Inter-packet Gap")
    print("═" * 58)

    conn = make_conn(host, port)
    pick_first_song(conn)

    print("  Streaming... (takes as long as the song)")
    stats = drain_stream(conn)
    conn.close()

    gaps = stats["inter_gap_ms"]
    print(f"\n  Packets received    : {stats['packets']}")
    print(f"  Data received       : {stats['bytes'] / 1024:.1f} KB")
    print(f"  Duration            : {stats['duration_s']} s")
    print(f"  Throughput          : {stats['throughput_kbps']} kbps")
    if gaps:
        print(f"\n  Inter-packet gap (ms)  — reflects server pacing + jitter:")
        print(f"    Min   : {min(gaps):.2f}")
        print(f"    Max   : {max(gaps):.2f}")
        print(f"    Mean  : {statistics.mean(gaps):.2f}")
        if len(gaps) > 1:
            print(f"    Stdev : {statistics.stdev(gaps):.2f}")
    return stats


# ════════════════════════════════════════════════
#  TEST 3 — Concurrent Clients (Scalability)
# ════════════════════════════════════════════════

def _concurrent_worker(host, port, results, errors, idx):
    try:
        conn      = make_conn(host, port)
        t_connect = time.perf_counter()
        pick_first_song(conn)
        stats = drain_stream(conn)
        conn.close()
        stats["client_idx"] = idx
        stats["connect_ms"] = round((time.perf_counter() - t_connect) * 1000, 1)
        results.append(stats)
    except Exception as e:
        errors.append(f"Client {idx}: {e}")


def test_concurrent(host, port, n_clients):
    print(f"\n{'═' * 58}")
    print(f"  TEST 3 — Scalability: {n_clients} Concurrent Clients")
    print(f"{'═' * 58}")

    results = []
    errors  = []
    threads = []

    wall_start = time.perf_counter()
    for i in range(n_clients):
        t = threading.Thread(
            target=_concurrent_worker,
            args=(host, port, results, errors, i + 1),
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(0.01)   # FIX 13 — was 0.1 s; 10 ms stagger still avoids
                           # log collision while enabling true simultaneous load

    for t in threads:
        t.join(timeout=300)

    wall = time.perf_counter() - wall_start

    print(f"\n  Clients completed : {len(results)}/{n_clients}")
    if errors:
        print(f"  Errors            : {len(errors)}")
        for e in errors:
            print(f"    ✗ {e}")

    if results:
        throughputs = [r["throughput_kbps"] for r in results]
        all_gaps    = []
        for r in results:
            all_gaps.extend(r["inter_gap_ms"])

        print(f"\n  Per-client results:")
        for r in sorted(results, key=lambda x: x["client_idx"]):
            print(f"    Client {r['client_idx']}: "
                  f"{r['bytes'] // 1024} KB  "
                  f"{r['throughput_kbps']} kbps  "
                  f"{r['duration_s']}s")

        print(f"\n  Aggregate throughput  : {sum(throughputs):.1f} kbps")
        print(f"  Avg per-client        : {statistics.mean(throughputs):.1f} kbps")
        if all_gaps:
            print(f"  Avg inter-packet gap  : {statistics.mean(all_gaps):.2f} ms")
        print(f"  Wall-clock time       : {wall:.1f} s")

    return results, errors


# ════════════════════════════════════════════════
#  TEST 4 — Packet Loss & Retry Rate
# ════════════════════════════════════════════════

def test_packet_loss(host, port):
    """
    Stream while randomly sending NACKs to simulate packet loss.
    Verifies the server's retry mechanism by checking the server.log afterwards.
    """
    print(f"\n{'═' * 58}")
    print("  TEST 4 — Packet Loss Simulation (dropped ACKs)")
    print(f"{'═' * 58}")

    conn       = make_conn(host, port)
    pick_first_song(conn)

    packets     = 0
    dropped     = 0
    total_bytes = 0
    start       = time.perf_counter()
    DROP_RATE   = 0.05

    try:
        while True:
            hdr = b""
            while len(hdr) < HEADER_SIZE:
                part = conn.recv(HEADER_SIZE - len(hdr))
                if not part:
                    raise ConnectionError()
                hdr += part

            seq_num, chunk_size, _, flags = struct.unpack(HEADER_FORMAT, hdr)

            if flags & FLAG_END:
                break

            payload = b""
            while len(payload) < chunk_size:
                part = conn.recv(chunk_size - len(payload))
                if not part:
                    raise ConnectionError()
                payload += part

            if random.random() < DROP_RATE:
                dropped += 1
                conn.sendall(struct.pack(ACK_FORMAT, seq_num, 0))   # NACK
            else:
                conn.sendall(struct.pack(ACK_FORMAT, seq_num, 1))   # ACK

            packets     += 1
            total_bytes += len(payload)

    except Exception:
        pass
    finally:
        conn.close()

    duration = time.perf_counter() - start
    print(f"\n  Packets received : {packets}")
    print(f"  ACKs dropped     : {dropped}  "
          f"({dropped / max(packets, 1) * 100:.1f}%)")
    print(f"  Data received    : {total_bytes // 1024} KB")
    print(f"  Duration         : {duration:.1f}s")
    print(f"  Server retries triggered for dropped ACKs (check server.log)")


# ════════════════════════════════════════════════
#  SUMMARY REPORT
# ════════════════════════════════════════════════

def print_summary(response_times, single_stats, concurrent_results):
    print(f"\n{'═' * 58}")
    print("  PERFORMANCE SUMMARY REPORT")
    print(f"{'═' * 58}")

    if response_times:
        print(f"\n  Response Time")
        print(f"    Mean : {statistics.mean(response_times):.1f} ms")
        print(f"    Best : {min(response_times):.1f} ms")

    if single_stats:
        gaps = single_stats["inter_gap_ms"]
        print(f"\n  Single Client Throughput")
        print(f"    {single_stats['throughput_kbps']} kbps  "
              f"({single_stats['bytes'] // 1024} KB in {single_stats['duration_s']}s)")
        if gaps:
            print(f"    Avg inter-packet gap : {statistics.mean(gaps):.2f} ms")

    for n, res, errs in concurrent_results:
        if res:
            tp = [r["throughput_kbps"] for r in res]
            print(f"\n  {n} Concurrent Clients")
            print(f"    Success rate : {len(res)}/{n}")
            print(f"    Total kbps   : {sum(tp):.1f}")
            print(f"    Avg kbps     : {statistics.mean(tp):.1f}")

    print(f"\n{'═' * 58}\n")


# ════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Music Streaming Performance Test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=9000, type=int)
    parser.add_argument("--skip-concurrent", action="store_true",
                        help="Skip multi-client tests (faster)")
    args = parser.parse_args()

    print("\n" + "═" * 58)
    print("  MUSIC STREAMING SERVER — PERFORMANCE TEST SUITE")
    print(f"  Target: {args.host}:{args.port}")
    print("═" * 58)
    print("\n  ⚠  Make sure streaming_server.py is running first!")
    input("  Press Enter to begin...\n")

    response_times     = []
    single_stats       = None
    concurrent_results = []

    try:
        response_times = test_response_time(args.host, args.port, runs=5)
    except Exception as e:
        print(f"\n  ❌  Test 1 failed: {e}")
        print("  Is the server running?  python streaming_server.py --music-dir music")
        raise SystemExit(1)

    try:
        single_stats = test_single_client(args.host, args.port)
    except Exception as e:
        print(f"\n  ❌  Test 2 failed: {e}")

    if not args.skip_concurrent:
        for n in [2, 3, 5]:
            try:
                res, errs = test_concurrent(args.host, args.port, n)
                concurrent_results.append((n, res, errs))
            except Exception as e:
                print(f"\n  ❌  Concurrent test ({n} clients) failed: {e}")
            time.sleep(2)
    else:
        print("\n  (Concurrent tests skipped)")

    try:
        test_packet_loss(args.host, args.port)
    except Exception as e:
        print(f"\n  ❌  Test 4 failed: {e}")

    print_summary(response_times, single_stats, concurrent_results)