"""
native_host.py

Native messaging host for the TCG Utils browser extension.

The browser (Chrome/Firefox) launches this process when the extension connects.
Communication is via stdin/stdout using the native messaging protocol:
  - 4-byte little-endian uint32 length prefix, then UTF-8 JSON body.

Messages from the extension:
  {"type": "start",      "config": {"watch_dir": "...", "output_dir": "...", "header_height": 2.5}}
  {"type": "stop"}
  {"type": "status"}
  {"type": "get_config"}

Messages sent to the extension:
  {"type": "status",  "running": bool, "watch_dir": "...", "output_dir": "...",
                      "header_height": float, "has_header": bool}
  {"type": "log",     "message": "...", "level": "info|warning|error"}
  {"type": "error",   "message": "..."}

Entry points (defined in pyproject.toml):
  tcg-host   — runs this native host (launched by the browser)
  tcg-setup  — one-time setup: registers the native host manifest with installed browsers
"""

import argparse
import json
import shutil
import struct
import sys
import threading
import traceback
from pathlib import Path

# Native messaging host name (must match extension manifest)
HOST_NAME = "com.twocorgistcg.host"

# Extension IDs allowed to connect
FIREFOX_EXTENSION_ID = "tcg-utils@twocorgistcg.com"
# Chrome extension ID — set by tcg-setup --chrome-id <id> or Chrome Web Store
CHROME_EXTENSION_ID_PLACEHOLDER = "CHROME_EXTENSION_ID_PLACEHOLDER"

_stdout_lock = threading.Lock()

DEFAULT_HEADER_INCHES = 2.5  # kept in sync with merge.py


def _set_binary_stdio() -> None:
    """On Windows, set stdin/stdout to binary mode to prevent CR/LF translation
    from corrupting the native messaging length-prefix protocol."""
    import platform
    if platform.system() == "Windows":
        import msvcrt
        import os
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


# ---------------------------------------------------------------------------
# Native messaging I/O
# ---------------------------------------------------------------------------


def _read_message() -> dict | None:
    """Read one native messaging message from stdin. Returns None on EOF."""
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    body = sys.stdin.buffer.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _send_message(msg: dict) -> None:
    """Write one native messaging message to stdout."""
    body = json.dumps(msg).encode("utf-8")
    with _stdout_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(body)))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    return Path.home() / ".tcg_utils" / "config.json"


def _load_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {
        "watch_dir": str(Path.home() / "Downloads"),
        "output_dir": str(Path.home() / "Documents" / "packing_slips"),
        "header_height": DEFAULT_HEADER_INCHES,
    }


def _save_config(cfg: dict) -> None:
    _config_path().write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Native host main loop
# ---------------------------------------------------------------------------


def main() -> None:
    _set_binary_stdio()

    # Log to file so startup errors are visible even if the protocol fails
    log_path = Path.home() / ".tcg_utils" / "host.log"
    log_path.parent.mkdir(exist_ok=True)
    _flog = open(log_path, "a", encoding="utf-8")

    def flog(msg: str) -> None:
        import datetime
        _flog.write(f"{datetime.datetime.now():%H:%M:%S}  {msg}\n")
        _flog.flush()

    flog("host started")

    # Lazy imports so any ImportError is caught and reported to the extension
    # rather than silently crashing the host process.
    try:
        from tcg_utils.merge import DEFAULT_HEADER_INCHES, get_header_cache
        from tcg_utils.watch import Watcher
        flog("imports OK")
    except Exception as exc:
        flog(f"import error: {exc}\n{traceback.format_exc()}")
        _send_message({
            "type": "error",
            "message": f"Failed to load tcg_utils: {exc}\n{traceback.format_exc()}",
        })
        return

    cfg = _load_config()
    watcher = None

    def log_fn(message: str, level: str = "info") -> None:
        _send_message({"type": "log", "message": message, "level": level})

    def current_status() -> dict:
        return {
            "type": "status",
            "running": watcher is not None and watcher.running,
            "watch_dir": cfg.get("watch_dir", ""),
            "output_dir": cfg.get("output_dir", ""),
            "header_height": cfg.get("header_height", DEFAULT_HEADER_INCHES),
            "has_header": get_header_cache().exists(),
        }

    while True:
        flog("waiting for message...")
        try:
            msg = _read_message()
        except Exception as exc:
            flog(f"read error: {exc}\n{traceback.format_exc()}")
            _send_message({"type": "error", "message": f"Read error: {exc}"})
            break

        if msg is None:
            flog("stdin closed — exiting")
            if watcher and watcher.running:
                watcher.stop()
            break

        flog(f"got message: {msg.get('type')}")
        msg_type = msg.get("type")

        if msg_type in ("get_config", "status"):
            _send_message(current_status())

        elif msg_type == "start":
            incoming = msg.get("config", {})
            if incoming:
                cfg.update(
                    {k: incoming[k] for k in ("watch_dir", "output_dir", "header_height") if k in incoming}
                )
                _save_config(cfg)

            if watcher and watcher.running:
                _send_message({"type": "log", "message": "Already running.", "level": "info"})
            else:
                if not get_header_cache().exists():
                    _send_message({
                        "type": "error",
                        "message": (
                            "No cached header found. "
                            "Run 'tcg-merge <csv> <template.pdf>' once to generate it."
                        ),
                    })
                else:
                    watcher = Watcher(
                        watch_dir=cfg["watch_dir"],
                        output_dir=cfg["output_dir"],
                        header_height_pts=cfg["header_height"] * 72,
                        log_fn=log_fn,
                    )
                    try:
                        watcher.start()
                        _send_message(current_status())
                    except Exception as e:
                        _send_message({"type": "error", "message": str(e)})
                        watcher = None

        elif msg_type == "stop":
            if watcher:
                watcher.stop()
                watcher = None
            _send_message(current_status())


# ---------------------------------------------------------------------------
# tcg-setup: register native messaging manifests with browsers
# ---------------------------------------------------------------------------


def _find_host_executable() -> str:
    """Locate the tcg-host executable. Raises if not found."""
    exe = shutil.which("tcg-host")
    if exe:
        return exe
    # Fallback: look next to the current script (editable installs / dev)
    sibling = Path(sys.argv[0]).parent / "tcg-host"
    for candidate in [sibling, sibling.with_suffix(".exe"), sibling.with_suffix(".cmd")]:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "Could not find 'tcg-host' in PATH. "
        "Make sure the package is installed: pip install ebay-tcg-utils"
    )


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))
    print(f"  Written: {path}")


def setup() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Register the TCG Utils native messaging host with Chrome and Firefox. "
            "Run this once after installing the package, then install the browser extension."
        )
    )
    parser.add_argument(
        "--chrome-id",
        metavar="EXTENSION_ID",
        help="Chrome/Edge extension ID (find it on chrome://extensions after loading the extension)",
    )
    args = parser.parse_args()

    import platform

    try:
        host_exe = _find_host_executable()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Host executable: {host_exe}")

    # --- Firefox ---
    firefox_manifest = {
        "name": HOST_NAME,
        "description": "TCG Utils native messaging host",
        "path": host_exe,
        "type": "stdio",
        "allowed_extensions": [FIREFOX_EXTENSION_ID],
    }

    import platform
    system = platform.system()

    if system == "Windows":
        firefox_nm_dir = Path.home() / "AppData" / "Roaming" / "Mozilla" / "NativeMessagingHosts"
    elif system == "Darwin":
        firefox_nm_dir = Path.home() / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
    else:
        firefox_nm_dir = Path.home() / ".mozilla" / "native-messaging-hosts"

    print("\nFirefox:")
    _write_manifest(firefox_nm_dir / f"{HOST_NAME}.json", firefox_manifest)

    # --- Chrome / Edge ---
    chrome_id = args.chrome_id or CHROME_EXTENSION_ID_PLACEHOLDER
    chrome_manifest = {
        "name": HOST_NAME,
        "description": "TCG Utils native messaging host",
        "path": host_exe,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{chrome_id}/"],
    }

    if system == "Windows":
        chrome_nm_dir = (
            Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts"
        )
        edge_nm_dir = (
            Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data" / "NativeMessagingHosts"
        )
    elif system == "Darwin":
        chrome_nm_dir = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
        edge_nm_dir = Path.home() / "Library" / "Application Support" / "Microsoft Edge" / "NativeMessagingHosts"
    else:
        chrome_nm_dir = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"
        edge_nm_dir = Path.home() / ".config" / "microsoft-edge" / "NativeMessagingHosts"

    print("\nChrome:")
    _write_manifest(chrome_nm_dir / f"{HOST_NAME}.json", chrome_manifest)

    print("\nEdge:")
    edge_manifest = {**chrome_manifest}
    _write_manifest(edge_nm_dir / f"{HOST_NAME}.json", edge_manifest)

    # Windows also requires registry entries for Chrome, Edge, and Firefox
    if system == "Windows":
        _register_windows_registry(
            chrome_nm_dir / f"{HOST_NAME}.json",
            firefox_nm_dir / f"{HOST_NAME}.json",
        )

    # Warn if Chrome ID was not provided
    if not args.chrome_id:
        print(
            "\nNote: Chrome/Edge were registered with a placeholder extension ID.\n"
            "To complete Chrome/Edge setup:\n"
            "  1. Load the extension in Chrome (chrome://extensions > Load unpacked)\n"
            "  2. Copy the Extension ID shown there\n"
            "  3. Run: tcg-setup --chrome-id <YOUR_EXTENSION_ID>\n"
            "(Firefox works immediately - no extra step needed.)"
        )
    else:
        print("\nSetup complete. Install the browser extension to get started.")


def _register_windows_registry(chrome_manifest: Path, firefox_manifest: Path) -> None:
    """Write Chrome, Edge, and Firefox registry keys on Windows."""
    try:
        import winreg

        entries = [
            (rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}", chrome_manifest),
            (rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}", chrome_manifest),
            (rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}", firefox_manifest),
        ]
        for key_path, manifest_path in entries:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
            print(f"  Registry: HKCU\\{key_path}")

    except Exception as e:
        print(f"  Warning: could not write registry keys: {e}")


if __name__ == "__main__":
    main()
