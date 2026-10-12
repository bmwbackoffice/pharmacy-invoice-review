"""
Start the review web app on the office LAN.

    python run_web.py

Serves http://0.0.0.0:PORT (plain HTTP — HTTPS comes with the later cloud move).
Prints the LAN URL colleagues use, and the exact Windows Firewall rule to allow
the port on the local network. All state is in PostgreSQL, so moving to a cloud
VM later = run this same command there against the same DATABASE_URL.

Env: DATABASE_URL (required), SECRET_KEY, PORT (default 8000),
     ADMIN_USER / ADMIN_PASSWORD (seed the first admin on first run).
"""

import os
import socket

import uvicorn

PORT = int(os.environ.get("PORT", "8000"))


def _lan_ip():
    """Best-effort primary LAN IPv4 of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the outbound iface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _port_in_use(port):
    """True if something is already bound to this port (another instance)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))  # no SO_REUSEADDR — we WANT this to fail if taken
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set — the web app needs the PostgreSQL database.")
    if os.environ.get("SECRET_KEY") in (None, "", "dev-insecure-change-me"):
        print("WARNING: SECRET_KEY is not set — using a dev key. Set SECRET_KEY for real logins.")

    if _port_in_use(PORT):
        raise SystemExit(
            f"\nPort {PORT} is already in use — another instance is probably still running.\n"
            f"  Stop it:  Get-NetTCPConnection -LocalPort {PORT} -State Listen | "
            f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}\n"
            f'  Or use another port:  $env:PORT="{PORT + 1}"; python run_web.py\n')

    ip = _lan_ip()
    # ALL-profile rule (no profile= => Domain + Private + Public), so LAN access survives the PC
    # switching networks / joining a domain — a profile-scoped rule silently stops applying when
    # the active profile changes, which looks like "the app is down" (it's just firewall-blocked).
    rule = (f'netsh advfirewall firewall add rule name="Invoice Review {PORT}" '
            f'dir=in action=allow protocol=TCP localport={PORT}')

    print("=" * 68)
    print("  Pharmacy Invoice Review — starting")
    print("=" * 68)
    print(f"  On this PC:        http://localhost:{PORT}")
    print(f"  Colleagues (LAN):  http://{ip}:{PORT}")
    print()
    print("  If colleagues can't reach it (times out), the Windows Firewall is blocking")
    print("  the port. In an ADMIN PowerShell / Command Prompt, run ONCE:")
    print()
    print("    " + rule)
    print()
    print("  (No profile= => applies to ALL network profiles, so it keeps working if this PC")
    print("   changes networks or joins a domain. On a domain-managed PC, IT may add it via")
    print("   Group Policy instead so it persists. To remove:")
    print(f'    netsh advfirewall firewall delete rule name="Invoice Review {PORT}")')
    print("=" * 68)

    # import here so config/errors above surface before the server boots
    uvicorn.run("review_app:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
