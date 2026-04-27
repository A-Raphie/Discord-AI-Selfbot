import os
import sys
import asyncio
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config_loader import get


def start_dashboard_thread():
    from dashboard.app import start_dashboard
    port = get("dashboard.port", 8080)
    start_dashboard(port=port)


def main():
    dashboard_thread = threading.Thread(target=start_dashboard_thread, daemon=True)
    dashboard_thread.start()

    from simple_bot import client, TOKEN
    client.run(TOKEN)


if __name__ == "__main__":
    main()
