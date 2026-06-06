#!/usr/bin/env python3
"""
launcher.py  —  Single-EXE, zero-folder launcher
==================================================
Runs all three components IN-PROCESS as threads/tasks:
  1. communication.py  → background thread  (TCP fleet manager)
  2. client.py         → background thread  (FastAPI / uvicorn)
  3. main.py           → main thread        (PyQt5 GUI — must own the main thread)

WHY THIS WORKS AS AN EXE
--------------------------
The old approach used subprocess to spawn `python script.py`.
That requires (a) Python installed on the client machine and
(b) the .py files to exist on disk — so you had to ship folders.

This approach imports the modules directly, so PyInstaller bundles
everything into the EXE and nothing needs to exist on disk at runtime.

BUILD COMMAND (run once, from final_fm/ folder):
  pyinstaller launcher.spec

DO NOT use --add-data for the Python source folders.
The .spec file adds FM_latest and server-client_code to pathex,
so PyInstaller compiles all .py files into the EXE — nothing is
shipped as readable source to clients.

Press Ctrl+C to shut everything down.
"""

import sys
import os
import threading
import time
import signal
import shutil
from pathlib import Path

# Force UTF-8 output so Unicode chars don't crash on Windows cp1252 console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------------------
# PATH SETUP
# ---------------------------------------------------------------------------
# When frozen by PyInstaller, sys._MEIPASS is the temp folder where all
# bundled files are extracted.  In dev mode it's just the source tree.
if getattr(sys, "frozen", False):
    # Running as compiled EXE
    BASE = os.path.dirname(sys.executable)
    BUNDLE = sys._MEIPASS  # type: ignore[attr-defined]
else:
    # Running as plain .py during development
    BASE = os.path.dirname(os.path.abspath(__file__))
    BUNDLE = BASE

# Add sub-project roots to sys.path so `import communication`, `import client`,
# and all of FM_latest's internal imports work correctly.
FM_ROOT     = os.path.join(BUNDLE, "FM_latest")
CLIENT_ROOT = os.path.join(BUNDLE, "server-client_code")

for p in (FM_ROOT, CLIENT_ROOT, BUNDLE):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# DATA DIRECTORY  (writable at runtime — sits next to the EXE, not inside it)
# ---------------------------------------------------------------------------
# PyInstaller's MEIPASS is read-only; user data must live next to the EXE.
# Guard against accidental nested data/data if EXE is run from a data folder.
if os.path.basename(BASE).lower() == "data":
    DATA_DIR = BASE
else:
    DATA_DIR = os.path.join(BASE, "data")

# If DATA_DIR is empty or missing critical files, look in FM_latest/data (for dev/first run)
if not os.path.exists(DATA_DIR) or not os.path.exists(os.path.join(DATA_DIR, "maps.csv")):
    alt_data = os.path.join(BASE, "FM_latest", "data")
    if os.path.exists(alt_data) and os.path.exists(os.path.join(alt_data, "maps.csv")):
        DATA_DIR = alt_data

os.makedirs(DATA_DIR, exist_ok=True)

# Inject the runtime data path so communication.py and client.py pick it up.
# Both scripts use os.environ or Path(__file__) — we override via env var.
os.environ.setdefault("WMS_DATA_DIR", DATA_DIR)


def _migrate_legacy_nested_data_dir(data_dir: str) -> None:
    """
    Move files from legacy nested '<data_dir>/data' into '<data_dir>'.
    This fixes old builds that wrote to dist/data/data/*.
    """
    legacy = Path(data_dir) / "data"
    target = Path(data_dir)
    if not legacy.exists() or not legacy.is_dir():
        return

    for src in legacy.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(legacy)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.move(str(src), str(dst))

    # Remove empty legacy tree.
    for d in sorted(legacy.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        legacy.rmdir()
        print(f"[LAUNCHER] Migrated legacy nested data folder: {legacy}")
    except OSError:
        pass


_migrate_legacy_nested_data_dir(DATA_DIR)

# ---------------------------------------------------------------------------
# SOFTWARE CONFIG BOOTSTRAP
# ---------------------------------------------------------------------------
# software_config.csv lives NEXT TO the EXE (dist/) so users can edit it and
# changes take effect immediately without rebuilding. On first run, we copy
# the bundled default template from _MEIPASS/_default_config/ if the file is
# missing. This means editing dist/software_config.csv always works.
_config_dest = os.path.join(BASE, "software_config.csv")
if not os.path.exists(_config_dest):
    _config_src = os.path.join(BUNDLE, "_default_config", "software_config.csv")
    if os.path.exists(_config_src):
        import shutil
        shutil.copy2(_config_src, _config_dest)


def _load_env_file(env_path: Path) -> None:
    """Load KEY=VALUE pairs from a simple .env file into process env."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _ensure_fernet_key(data_dir: str) -> None:
    """
    Ensure FERNET_KEY exists at runtime.
    Priority:
      1) existing process env
      2) BASE/.env
      3) DATA_DIR/.env
    If still missing, generate one and persist to DATA_DIR/.env.
    """
    base_env = Path(BASE) / ".env"
    data_env = Path(data_dir) / ".env"

    _load_env_file(base_env)
    _load_env_file(data_env)

    if os.environ.get("FERNET_KEY"):
        return

    from cryptography.fernet import Fernet

    generated_key = Fernet.generate_key().decode("utf-8")
    os.environ["FERNET_KEY"] = generated_key

    with data_env.open("a", encoding="utf-8") as f:
        if data_env.stat().st_size > 0:
            f.write("\n")
        f.write("# Auto-generated on first launch\n")
        f.write(f"FERNET_KEY={generated_key}\n")

    print(f"[LAUNCHER] Generated runtime FERNET_KEY at {data_env}")


_ensure_fernet_key(DATA_DIR)

# ---------------------------------------------------------------------------
# 1. FLEET MANAGER (communication.py)  — runs in a daemon thread
# ---------------------------------------------------------------------------
def start_fleet_manager_thread():
    """Import and run start_fleet_manager() from communication.py in a thread."""
    try:
        import communication
        print("[LAUNCHER] Starting Fleet Manager (communication.py)…")
        communication.start_fleet_manager()
    except Exception as e:
        print(f"[LAUNCHER] Fleet Manager error: {e}")

# ---------------------------------------------------------------------------
# 2. CLIENT / FastAPI SERVER (client.py)  — runs in a daemon thread via uvicorn
# ---------------------------------------------------------------------------
def start_client_server_thread():
    """Import the FastAPI app from client.py and serve it with uvicorn."""
    try:
        import uvicorn
        # Import the module so uvicorn can reference it.
        # client.py defines `app = FastAPI(...)` at module level.
        import client as client_module
        print("[LAUNCHER] Starting Client API Server (client.py on port 8000)…")
        uvicorn.run(
            client_module.app,
            host="0.0.0.0",
            port=8000,
            log_level="info",
        )
    except Exception as e:
        print(f"[LAUNCHER] Client Server error: {e}")

# ---------------------------------------------------------------------------
# 3. PyQt5 GUI (main.py)  — must run on the MAIN thread
# ---------------------------------------------------------------------------
def run_gui():
    """Import and run the PyQt5 application from main.py."""
    # main.py defines main() which creates QApplication and enters exec_().
    import main as main_module
    print("[LAUNCHER] Starting Warehouse GUI (main.py)…")
    return main_module.main()

# ---------------------------------------------------------------------------
# SHUTDOWN HANDLER
# ---------------------------------------------------------------------------
def shutdown(signum=None, frame=None):
    print("\n[LAUNCHER] Shutting down…")
    # Daemon threads will die automatically when the main thread exits.
    sys.exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[LAUNCHER] =====================================")
    print("[LAUNCHER]  Warehouse System - Single EXE    ")
    print(f"[LAUNCHER]  BASE    = {BASE}")
    print(f"[LAUNCHER]  FM_ROOT = {FM_ROOT}")
    print(f"[LAUNCHER]  DATA    = {DATA_DIR}")
    print("[LAUNCHER] =====================================")

    # Start Fleet Manager in background daemon thread
    t1 = threading.Thread(target=start_fleet_manager_thread, daemon=True, name="FleetManager")
    t1.start()

    # Give the TCP server a moment to bind before the GUI starts
    time.sleep(1)

    # Start FastAPI / uvicorn in background daemon thread
    t2 = threading.Thread(target=start_client_server_thread, daemon=True, name="ClientAPI")
    t2.start()

    # Give uvicorn a moment to spin up
    time.sleep(1)

    # Run the GUI on the main thread — this blocks until the window is closed
    exit_code = run_gui()

    print("[LAUNCHER] GUI closed. Exiting.")
    sys.exit(exit_code if exit_code is not None else 0)
