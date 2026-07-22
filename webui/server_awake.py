from __future__ import annotations

import ctypes
import threading
import time

from waitress import serve

from workflow.single_instance import SingleInstanceError, acquire_webui_instance_lock


# Windows SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake_loop() -> None:
    """
    Keep the Windows laptop awake while this server is running.
    No admin permission required.
    """
    while True:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        time.sleep(30)


if __name__ == "__main__":
    try:
        acquire_webui_instance_lock()
    except SingleInstanceError as exc:
        raise SystemExit(str(exc)) from exc

    from app import app

    threading.Thread(target=keep_awake_loop, daemon=True).start()

    serve(
        app,
        host="0.0.0.0",
        port=5055,
        threads=4,
    )
