import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config_loader import get


def start_dashboard_thread():
    from dashboard.app import start_dashboard
    port = int(os.environ.get("PORT", get("dashboard.port", 8080)))
    start_dashboard(port=port)


def main():
    dashboard_thread = threading.Thread(target=start_dashboard_thread, daemon=True)
    dashboard_thread.start()
    time.sleep(2)

    import simple_bot
    try:
        simple_bot.init_ai_client()
        simple_bot.logger.info("Connecting to Discord...")
        simple_bot.client.run(simple_bot.TOKEN)
    except Exception as e:
        simple_bot.logger.error(f"Bot crashed: {e}")
        simple_bot.logger.info("Dashboard still running on background thread")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
