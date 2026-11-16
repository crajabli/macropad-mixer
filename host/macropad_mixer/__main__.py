"""Command-line entry point: `macropad-mixer` / `python -m macropad_mixer`.

    macropad-mixer [options]                    run the daemon
    macropad-mixer autostart install [options]  start it at every login (options are passed on)
    macropad-mixer autostart uninstall|status
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from .backends.base import BACKENDS, load_backend

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _autostart(argv: list[str]) -> None:
    from . import autostart

    actions = ("install", "uninstall", "status")
    if not argv or argv[0] not in actions:
        sys.exit(f"usage: macropad-mixer autostart {{{'|'.join(actions)}}} [daemon options]")
    try:
        print(autostart.run(argv[0], argv[1:]))
    except Exception as e:  # noqa: BLE001 - report, don't traceback, for a CLI action
        sys.exit(f"autostart {argv[0]} failed: {e}")


def _setup_logging(verbose: bool, log_file: str | None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])
    logging.getLogger("PIL").setLevel(logging.INFO)  # its PNG chunk debug output is noise


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["autostart"]:
        _autostart(argv[1:])
        return

    ap = argparse.ArgumentParser(prog="macropad-mixer", description="MacroPad per-app volume mixer daemon",
                                 epilog="Run `macropad-mixer autostart install` to start it at every login.")
    ap.add_argument("--port", help="serial port of the MacroPad data channel (default: auto-detect)")
    ap.add_argument("--step", type=int, default=5, help="volume percent per knob click (default: 5)")
    ap.add_argument("--backend", choices=BACKENDS, default="auto", help="audio backend (default: auto)")
    ap.add_argument("--bright-step", type=int, default=5,
                    help="brightness percent per knob click on key 12 (default: 5)")
    ap.add_argument("--forget-after", type=float, default=60.0,
                    help="seconds of silence before an unpinned app leaves its key (default: 60)")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between app list polls (default: 2)")
    ap.add_argument("--log-file", help="write logs to this file (rotated at 1 MB) instead of the terminal")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)
    _setup_logging(args.verbose, args.log_file)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # launchd/logout: clean shutdown

    from .link import SerialLink
    from .mixer import Mixer
    from .displays.base import load_display_backend
    from .pins import Pins, default_path

    backend = load_backend(args.backend)
    link = SerialLink(args.port)
    displays = load_display_backend()
    from .icons import default_store_dir

    mixer = Mixer(backend, link, step=args.step, poll=args.poll, pins=Pins(default_path()),
                  icon_store=default_store_dir(),
                  displays=displays, bright_step=args.bright_step, forget_after=args.forget_after)
    logging.getLogger(__name__).info("macropad-mixer started (backend %s)", type(backend).__name__)
    try:
        mixer.run()
    except KeyboardInterrupt:
        pass
    finally:
        if mixer.icons:
            mixer.icons.close()
        link.close()
        backend.close()
        displays.close()


if __name__ == "__main__":
    main()
