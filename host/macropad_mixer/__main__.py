"""Command-line entry point: `macropad-mixer` / `python -m macropad_mixer`."""

from __future__ import annotations

import argparse
import logging

from .backends.base import BACKENDS, load_backend


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="macropad-mixer", description="MacroPad per-app volume mixer daemon")
    ap.add_argument("--port", help="serial port of the MacroPad data channel (default: auto-detect)")
    ap.add_argument("--step", type=int, default=5, help="volume percent per knob click (default: 5)")
    ap.add_argument("--backend", choices=BACKENDS, default="auto", help="audio backend (default: auto)")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between app list polls (default: 2)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .link import SerialLink
    from .mixer import Mixer

    backend = load_backend(args.backend)
    link = SerialLink(args.port)
    try:
        Mixer(backend, link, step=args.step, poll=args.poll).run()
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        backend.close()


if __name__ == "__main__":
    main()
