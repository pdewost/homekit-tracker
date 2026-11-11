"""Example notifier adapter. Copy this file to `scripts/local_notifier.py` (git-ignored) and edit it.

The tracker loads `scripts/local_notifier.py` when it exists and calls `notifier()`, which returns a function
`send(text) -> bool`. The text holds fixed codes and counts only. The adapter runs inside the tracker's process,
so it has the tracker's Full Disk Access: keep it short, and keep any address or token out of this example.
Never log an exception text from here: it may hold the address.
"""

import os
from typing import Callable


def notifier() -> Callable[[str], bool]:
    target = os.environ.get("HOME_AUTO_NOTIFY_TO", "")  # replace with your own delivery code

    def send(text: str) -> bool:
        if not target:
            return False
        # deliver `text` to `target` here, and return True once it is sent
        return False
    return send
