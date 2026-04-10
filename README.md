# 🎵 Online Music Streaming Server
### Jackfruit Mini Project — Socket Programming (Project #9)

---

## Overview

A **secure, multi-client music streaming server** built entirely with raw TCP sockets and SSL/TLS encryption.  
The server decodes audio files to PCM on the server side and streams them in real time to multiple clients simultaneously, with adaptive bitrate control based on live network conditions.

| Feature | Details |
|---|---|
| Protocol | TCP (raw sockets — no frameworks) |
| Security | SSL/TLS (TLS 1.2+, self-signed cert) |
| Concurrency | Multi-threaded — one thread per client |
| Audio Formats | WAV, MP3, FLAC, OGG, AAC, M4A (via pydub) |
| Adaptive Streaming | RTT + packet-loss based bitrate switching |
| Buffer Management | Server-side `StreamBuffer` + client-side chunk queue |
| Packet Loss Handling | ACK/NACK protocol with up to 5 retries per packet |
| QoS Evaluation | Per-session throughput, latency, loss rate, bitrate history |
| Language | Python 3.8+ |

---

## File Structure

```
music_streaming/
├── streaming_server.py     ← Main server (Deliverable 1 + 2)
├── streaming_client.py     ← Interactive client with real-time audio playback
├── performance_test.py     ← Deliverable 2: performance evaluation suite
├── server.crt              ← SSL certificate (auto-generated on first run)
├── server.key              ← SSL private key  (auto-generated on first run)
├── server.log              ← Server logs (created at runtime)
└── music/                  ← Put your .wav / .mp3 files here
    ├── song1.wav
    └── ...
```

---

## Setup

### 1. Install dependencies

```bash
pip install pydub cryptography pyaudio
```

> **Windows users:** if `pyaudio` fails, try:
> ```
> pip install pipwin
> pipwin install pyaudio
> ```

### 2. Install ffmpeg (for MP3 / FLAC / OGG support)

- Download from https://ffmpeg.org/download.html  
- Add the `bin\` folder to your system PATH  
- WAV files work without ffmpeg

### 3. Add music files

Create a `music\` folder and copy `.wav` or `.mp3` files into it.

---

## Running the Project

### Terminal 1 — Start the Server

```bash
python streaming_server.py --music-dir music
```

The server will **auto-generate SSL certificates** on the first run.  
Expected output:
```
[INFO] SSL certificates found.
[INFO] Song library (3 songs):
[INFO]    [ 1] song1.wav  (3420 KB)
[INFO] Listening on 0.0.0.0:9000 (TLS enabled)
```

### Terminal 2 — Start the Client

```bash
python streaming_client.py --host 127.0.0.1 --port 9000
```

Pick a song number and hear it play live through your speakers.

### Terminal 3+ — Additional Clients (multi-client demo)

```bash
python streaming_client.py --host 127.0.0.1 --port 9000
```

Each client runs in its own thread on the server — stream simultaneously.

---

## Performance Evaluation (Deliverable 2)

With the server running, open a new terminal and run:

```bash
python performance_test.py
```

This runs 4 automated tests and prints a summary report:

| Test | What it measures |
|---|---|
| **Test 1 — Response Time** | Time from connect → first byte (5 runs, mean/min/max) |
| **Test 2 — Throughput & Latency** | KB/s and ACK round-trip time for a single client |
| **Test 3 — Concurrent Clients** | 2, 3, and 5 clients simultaneously — per-client and total throughput |
| **Test 4 — Packet Loss** | Simulates 5% ACK drops, verifies server retry behaviour |

---

## Architecture

```
CLIENT                              SERVER
  │                                    │
  │── TCP Connect ──────────────────>  │
  │<─ TLS 1.2 Handshake ─────────────  │
  │                                    │
  │── JSON: {id: 2} ────────────────>  │  Song selection
  │<─ JSON: {status:ok, song:...} ───  │  + audio format info
  │                                    │
  │<══ [HDR|PCM chunk] ══════════════  │  Binary stream (length-prefixed)
  │── ACK ──────────────────────────>  │  Per-packet acknowledgement
  │<══ [HDR|PCM chunk] ══════════════  │
  │── ACK ──────────────────────────>  │
  │          ...                       │
  │<─ [FLAG_END] ────────────────────  │  End of song
  │                                    │
  │── JSON: {id: 3} ────────────────>  │  Pick next song or quit
```

### Packet Header Format

```
 ┌──────────────┬──────────────┬─────────────┬───────────┐
 │  seq_num (4B)│ chunk_size(2B)│ bitrate (1B)│ flags (1B)│
 └──────────────┴──────────────┴─────────────┴───────────┘
 Flags: 0x01=DATA  0x02=END  0x04=RESEND  0x08=PING
```

### Adaptive Bitrate Logic

| Condition | Action |
|---|---|
| loss > 5% OR avg RTT > 500ms | Downgrade bitrate (level - 1) |
| loss < 2% AND avg RTT < 200ms AND ≥5 samples | Upgrade bitrate (level + 1) |

Bitrate re-evaluated every 10 packets.

---

## Deliverable 2 — Optimizations & Fixes

All changes are marked with `# Deliverable 2 — Optimization` in `streaming_server.py`.

| Issue | Fix |
|---|---|
| `openssl` command not available on Windows | Replaced with pure-Python cert generation using `cryptography` library |
| Abrupt client disconnections caused unhandled exceptions | `ConnectionResetError` / `BrokenPipeError` caught at both session and stream level |
| SSL handshake failures crashed the handler thread | Specific `ssl.SSLError` catch in `_handle_client` with descriptive logging |
| Malformed / invalid JSON from client | `_recv_json` now validates JSON, guards against oversized inputs |
| Mid-stream partial failures (OSError) | Caught separately in `run()` with clean teardown |
| Invalid song ID sent by client | Returns a clear error JSON instead of `None` path crash |

---

## Command-line Options

### Server

```
python streaming_server.py --help

  --host        Bind address (default: 0.0.0.0)
  --port        Port (default: 9000)
  --music-dir   Path to folder containing audio files (default: .)
  --cert        SSL certificate path (default: server.crt)
  --key         SSL key path (default: server.key)
```

### Client

```
python streaming_client.py --help

  --host        Server hostname or IP (default: 127.0.0.1)
  --port        Server port (default: 9000)
  --output-dir  Directory to save received audio (default: .)
```

### Performance Test

```
python performance_test.py --help

  --host              Server host (default: 127.0.0.1)
  --port              Server port (default: 9000)
  --skip-concurrent   Skip multi-client tests (run faster)
```

---

## GitHub Upload

```bash
git init
git add streaming_server.py streaming_client.py performance_test.py README.md
echo "server.key" >> .gitignore
echo "*.log" >> .gitignore
git commit -m "Socket Programming Project - Online Music Streaming Server"
git remote add origin https://github.com/YOUR_USERNAME/music-streaming
git branch -M main
git push -u origin main
```

> Add `server.key` to `.gitignore` — never push private keys to GitHub.