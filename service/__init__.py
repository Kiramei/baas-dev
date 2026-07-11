def set_log_format():
    """Install the rich console formatter used by service-mode startup logs."""
    import logging
    from datetime import datetime
    from rich.console import Console
    from rich.markup import escape

    console = Console()

    levels_label = {
        logging.INFO: ("[INFO]", "#2d8cf0"),
        logging.WARNING: ("[WARN]", "#ff9900"),
        logging.ERROR: ("[ERRO]", "#ed3f14"),
        logging.CRITICAL: ("[CRIT]", "#7c3aed"),
    }

    class RichFormatter(logging.Formatter):
        def format(self, record):
            label, color = levels_label.get(record.levelno, ("INFO", "cyan"))
            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = escape(record.getMessage())
            level_tag = f"[{color} bold]{label}[/]"
            time_tag = f"[dim]{time_str}[/dim]"
            if record.levelno != logging.INFO:
                msg = f"[{color} bold]{msg}[/]"
            return f"{time_tag}  {level_tag}  {msg}"

    # ----------- Handler 直接输出到 console.print -----------
    class RichHandler(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
                console.print(msg, soft_wrap=True)
            except Exception:
                self.handleError(record)

    # ----------- 使用 ----------
    handler = RichHandler()
    handler.setFormatter(RichFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]

    logging.getLogger("watchfiles").setLevel(logging.ERROR)
    logging.getLogger("watchfiles.main").setLevel(logging.ERROR)


import warnings
import os
import sys

# Suppress warning from adbutils
warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API",
    category=UserWarning
)

from .injection import prepare_service_imports


def _shared_memory_transport_requested() -> bool:
    if os.getenv("BAAS_SERVICE_TRANSPORT", "").strip().lower() == "shm":
        return True
    argv = list(sys.argv[1:])
    for index, value in enumerate(argv):
        if value == "--transport" and index + 1 < len(argv) and argv[index + 1] == "shm":
            return True
        if value == "--transport=shm":
            return True
    return False


if not _shared_memory_transport_requested():
    prepare_service_imports()


def __getattr__(name):
    """Load service application objects only when callers explicitly request them."""
    if name in {"app", "context"}:
        from .app import app, context

        values = {
            "app": app,
            "context": context,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["app", "context", "set_log_format"]
