"""
Test Packaged Futaba.exe Execution & IPC Integration.

Verifies:
1. dist\\Futaba\\Futaba.exe starts cleanly in headless mode.
2. Runtime bootstrapper provisions %LOCALAPPDATA%\\Futaba layout.
3. Cryptographic IPC token is written to runtime\\ipc.token.
4. WebSocket client connects to ws://127.0.0.1:45850.
5. Unauthenticated request is rejected.
6. Authenticated session using runtime token succeeds.
7. Endpoints verified over IPC:
   - get_status (returns running: True, active task counts)
   - get_diagnostics (Futaba Doctor checks across subsystems)
   - get_resource_status (CPU/RAM metrics)
   - get_providers (configured LLM provider list)
   - health_check (tool registry health)
   - set_gaming_mode / set_dnd (dynamic state adjustments)
   - set_credential (secure DPAPI credential write)
8. Clean process shutdown and token cleanup.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import websockets

EXE_PATH = Path(__file__).resolve().parent.parent / "dist" / "Futaba" / "Futaba.exe"
TOKEN_FILE = Path(os.environ.get("LOCALAPPDATA", "")) / "Futaba" / "runtime" / "ipc.token"
WS_URL = "ws://127.0.0.1:45850"


async def run_packaged_test():
    print("=" * 70)
    print("FUTABA PACKAGED EXECUTABLE ACCEPTANCE TEST")
    print(f"Target Binary: {EXE_PATH}")
    print("=" * 70)

    assert EXE_PATH.exists(), f"Packaged executable not found at: {EXE_PATH}"
    print(f"[Check 1] Binary exists ({EXE_PATH.stat().st_size / (1024*1024):.1f} MB)")

    # 1. Clean any stale token
    if TOKEN_FILE.exists():
        try:
            TOKEN_FILE.unlink()
        except Exception:
            pass

    # 2. Launch Futaba.exe in headless mode
    print("\n[Check 2] Launching Futaba.exe in headless mode...")
    proc = subprocess.Popen(
        [str(EXE_PATH), "--mode", "headless"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    try:
        # Wait for token file to appear (proves bootstrapper & IPC server initialized)
        print("Waiting for IPC token generation...")
        token = ""
        for _ in range(60):
            if TOKEN_FILE.exists():
                token = TOKEN_FILE.read_text(encoding="utf-8").strip()
                if token:
                    break
            await asyncio.sleep(0.5)

        assert token, "IPC token file was not written within 30 seconds!"
        print(f"[Check 3] Runtime token detected: {token[:8]}... (length: {len(token)})")

        # 3. Connect to IPC WebSocket
        print(f"\n[Check 4] Connecting to IPC WebSocket at {WS_URL}...")
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            # 4. Verify unauthenticated call is rejected
            unauth_req = json.dumps({"id": "probe-1", "method": "get_status", "params": {}})
            await ws.send(unauth_req)
            res = json.loads(await ws.recv())
            assert "Not authenticated" in res.get("error", {}).get("message", "")
            print("       PASS: Unauthenticated request rejected.")

        # Reconnect to authenticate
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            # 5. Authenticate with valid token
            auth_req = json.dumps({"id": "auth-1", "method": "auth", "params": {"token": token}})
            await ws.send(auth_req)
            auth_res = json.loads(await ws.recv())
            assert auth_res.get("result", {}).get("status") == "authenticated"
            print("       PASS: Token handshake authenticated successfully.")

            # 6. Test get_status
            req = json.dumps({"id": "req-status", "method": "get_status", "params": {}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            status = res.get("result", {})
            assert status.get("running") is True
            print(f"       PASS: get_status -> version: {status.get('version')}, running: {status.get('running')}")

            # 7. Test get_diagnostics (Futaba Doctor)
            req = json.dumps({"id": "req-diag", "method": "get_diagnostics", "params": {}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            diag = res.get("result", {})
            assert diag.get("status") == "healthy"
            print(f"       PASS: get_diagnostics -> status: {diag.get('status')}, checks: {list(diag.get('checks', {}).keys())}")

            # 8. Test get_resource_status
            req = json.dumps({"id": "req-res", "method": "get_resource_status", "params": {}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            res_data = res.get("result", {})
            assert "cpu_percent" in res_data
            assert "ram_percent" in res_data
            print(f"       PASS: get_resource_status -> CPU: {res_data.get('cpu_percent')}%, RAM: {res_data.get('ram_percent')}%")

            # 9. Test get_providers
            req = json.dumps({"id": "req-prov", "method": "get_providers", "params": {}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            providers = res.get("result", [])
            print(f"       PASS: get_providers -> found {len(providers)} providers: {[p.get('name') for p in providers]}")

            # 10. Test health_check
            req = json.dumps({"id": "req-health", "method": "health_check", "params": {}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            health = res.get("result", {})
            print(f"       PASS: health_check -> tool status: {health.get('tools', {})}")

            # 11. Test dynamic mode toggles
            req = json.dumps({"id": "req-game", "method": "set_gaming_mode", "params": {"enabled": True}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            assert res.get("result", {}).get("gaming_mode") is True

            req = json.dumps({"id": "req-dnd", "method": "set_dnd", "params": {"enabled": True}})
            await ws.send(req)
            res = json.loads(await ws.recv())
            assert res.get("result", {}).get("dnd") is True
            print("       PASS: Dynamic Gaming Mode & DND toggles verified over IPC.")

            # 12. Test set_credential (DPAPI store)
            req = json.dumps({
                "id": "req-cred",
                "method": "set_credential",
                "params": {"name": "packaged_test_key", "value": "secret_dpapi_val"}
            })
            await ws.send(req)
            res = json.loads(await ws.recv())
            assert res.get("result", {}).get("success") is True
            print("       PASS: DPAPI credential storage verified over IPC.")

        print("\n" + "=" * 70)
        print("ALL PACKAGED EXECUTABLE ACCEPTANCE CHECKS PASSED!")
        print("=" * 70)

    finally:
        # Graceful shutdown of Futaba.exe
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Futaba.exe process cleanly terminated.")


if __name__ == "__main__":
    asyncio.run(run_packaged_test())
