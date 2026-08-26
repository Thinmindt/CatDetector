"""Shared logging configuration for the entry-point scripts."""

import logging


def configure_logging(*app_loggers: str) -> None:
    """Timestamped records to stderr: this application at INFO, the rest at WARNING."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("src", *app_loggers):
        logging.getLogger(name).setLevel(logging.INFO)
