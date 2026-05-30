"""
Convenience launcher for local development.

Spawns the RPG_Engine MCP server (SSE on :8001), waits for it to bind,
then starts uvicorn (FastAPI on :8000). Both children share this
terminal — easy for development.

For demos / grading, prefer the visible two-terminal flow so a viewer
can see the MCP server's own log output as it serves tool calls:

    Terminal 1:   python server.py
    Terminal 2:   uvicorn app:app --reload
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
MCP_HOST = "127.0.0.1"
MCP_PORT = 8001


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    print(f"[run.py] starting RPG_Engine MCP server on {MCP_HOST}:{MCP_PORT}...")
    mcp_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=str(ROOT),
    )

    if not _wait_for_port(MCP_HOST, MCP_PORT, timeout=15.0):
        print("[run.py] MCP server failed to bind; aborting.", file=sys.stderr)
        mcp_proc.terminate()
        mcp_proc.wait(timeout=5)
        return 1

    print("[run.py] starting uvicorn on 127.0.0.1:8000...")
    uvi_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app",
         "--host", "127.0.0.1", "--port", "8000", "--reload"],
        cwd=str(ROOT),
    )

    try:
        uvi_proc.wait()
    except KeyboardInterrupt:
        print("\n[run.py] Ctrl-C received, shutting both processes down...")
    finally:
        for p in (uvi_proc, mcp_proc):
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGINT)
                except Exception:
                    p.terminate()
        for p in (uvi_proc, mcp_proc):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    return uvi_proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
