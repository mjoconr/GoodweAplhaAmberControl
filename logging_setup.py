#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _level_from_str(level: str) -> int:
    s = str(level or "").strip().upper()
    if not s:
        return logging.INFO
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return logging.INFO
    return logging._nameToLevel.get(s, logging.INFO)


def setup_logging(app_name: str, debug_default: bool = False) -> logging.Logger:
    """Configure rotating file + optional stdout logging.

    Environment variables:
      LOG_DIR=logs
      LOG_LEVEL=INFO|DEBUG|...
      LOG_TO_STDOUT=1
      LOG_TO_FILE=1
      LOG_MAX_BYTES=5242880
      LOG_BACKUP_COUNT=5
      LOG_UTC=0
      LOG_FORMAT='[%(asctime)s] %(message)s'
      LOG_DATEFMT='%Y-%m-%dT%H:%M:%S'

    Each process writes to ${LOG_DIR}/{app_name}.log unless LOG_FILE is set.
    """

    log_dir = Path(os.getenv("LOG_DIR", "logs")).expanduser()
    log_file_env = os.getenv("LOG_FILE", "").strip()
    log_path = Path(log_file_env).expanduser() if log_file_env else (log_dir / f"{app_name}.log")

    level = os.getenv("LOG_LEVEL", "").strip()
    if not level:
        level = "DEBUG" if debug_default else "INFO"
    level_num = _level_from_str(level)

    to_stdout = _env_bool("LOG_TO_STDOUT", True)
    to_file = _env_bool("LOG_TO_FILE", True)

    max_bytes = _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
    backup_count = _env_int("LOG_BACKUP_COUNT", 5)

    use_utc = _env_bool("LOG_UTC", False)
    fmt = os.getenv("LOG_FORMAT", "[%(asctime)s] %(message)s")
    datefmt = os.getenv("LOG_DATEFMT", "%Y-%m-%dT%H:%M:%S")

    # Root logger so library loggers (requests/uvicorn/etc) can flow through too.
    root = logging.getLogger()
    root.setLevel(level_num)

    # Remove handlers if this is re-run (e.g., in tests or reloads)
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    if use_utc:
        formatter.converter = time.gmtime

    handlers: list[logging.Handler] = []

    if to_file:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                filename=str(log_path),
                maxBytes=max(0, int(max_bytes)),
                backupCount=max(0, int(backup_count)),
                encoding="utf-8",
                delay=True,
            )
            fh.setLevel(level_num)
            fh.setFormatter(formatter)
            handlers.append(fh)
        except Exception:
            # If file handler fails (permissions/disk), fall back to stdout only.
            pass

    if to_stdout:
        sh = logging.StreamHandler()
        sh.setLevel(level_num)
        sh.setFormatter(formatter)
        handlers.append(sh)

    # If both failed, still avoid a "No handlers" warning by adding a StreamHandler.
    if not handlers:
        sh = logging.StreamHandler()
        sh.setLevel(level_num)
        sh.setFormatter(formatter)
        handlers.append(sh)

    for h in handlers:
        root.addHandler(h)

    logging.captureWarnings(True)

    return logging.getLogger(app_name)
