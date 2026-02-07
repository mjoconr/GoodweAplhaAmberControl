#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import logging

from logging_setup import setup_logging


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

# Logging
DEBUG = _env_bool("DEBUG", False)
LOG = setup_logging("ingest", debug_default=DEBUG)



def _wal_path(db_path: Path) -> Path:
    # SQLite WAL file naming is "<db>-wal"
    return Path(str(db_path) + "-wal")


def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Keep one writer connection open for performance, but ensure WAL checkpoints happen.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Better concurrency characteristics for a read-heavy UI process.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Reduce the surprise of a tiny main DB file with a large -wal file.
    # Default SQLite autocheckpoint is 1000 pages (~4MB at 4KB pages). We use a smaller default.
    conn.execute(f"PRAGMA wal_autocheckpoint={_env_int('INGEST_WAL_AUTOCHECKPOINT_PAGES', 200)}")

    # Avoid transient lock failures if the API/UI hit the DB at the same time.
    conn.execute(f"PRAGMA busy_timeout={_env_int('INGEST_BUSY_TIMEOUT_MS', 5000)}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            ts_utc TEXT,
            ts_local TEXT,
            ts_epoch_ms INTEGER,
            host TEXT,
            pid INTEGER,
            loop INTEGER,
            export_costs INTEGER,
            want_pct INTEGER,
            want_enabled INTEGER,
            reason TEXT,
            slimmed INTEGER DEFAULT 0,
            data_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_epoch_ms ON events(ts_epoch_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id)")

    try:
        conn.execute("ALTER TABLE events ADD COLUMN slimmed INTEGER DEFAULT 0")
    except Exception:
        # older DB already has the column
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_slimmed ON events(slimmed)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_notes (
            event_id TEXT PRIMARY KEY,
            note TEXT,
            updated_ts_utc TEXT
        )
        """
    )

    conn.commit()
    return conn


def _extract_columns(event: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    export_costs = decision.get("export_costs")
    want_pct = decision.get("want_pct")
    want_enabled = decision.get("want_enabled")
    reason = decision.get("reason")

    cols = {
        "event_id": event.get("event_id"),
        "ts_utc": event.get("ts_utc"),
        "ts_local": event.get("ts_local"),
        "ts_epoch_ms": event.get("ts_epoch_ms"),
        "host": event.get("host"),
        "pid": event.get("pid"),
        "loop": event.get("loop"),
        "export_costs": 1 if export_costs else 0 if export_costs is not None else None,
        "want_pct": int(want_pct) if want_pct is not None else None,
        "want_enabled": int(want_enabled) if want_enabled is not None else None,
        "reason": str(reason) if reason is not None else None,
    }

    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return cols, payload


def _ingest_one(conn: sqlite3.Connection, json_path: Path) -> bool:
    """Return True if the file was valid and handled (inserted or already present)."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            event = json.load(f)
        if not isinstance(event, dict):
            return False

        cols, payload = _extract_columns(event)
        if not cols.get("event_id"):
            return False

        # Treat duplicates as success so we still move/delete the file.
        conn.execute(
            """
            INSERT INTO events(
                event_id, ts_utc, ts_local, ts_epoch_ms, host, pid, loop,
                export_costs, want_pct, want_enabled, reason, data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                cols.get("event_id"),
                cols.get("ts_utc"),
                cols.get("ts_local"),
                cols.get("ts_epoch_ms"),
                cols.get("host"),
                cols.get("pid"),
                cols.get("loop"),
                cols.get("export_costs"),
                cols.get("want_pct"),
                cols.get("want_enabled"),
                cols.get("reason"),
                payload,
            ),
        )
        conn.commit()
        return True
    except Exception:
        return False


def _move_processed(src: Path, processed_dir: Path) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    dst = processed_dir / src.name
    # If dst exists (duplicate), append .dup + epoch
    if dst.exists():
        dst = processed_dir / f"{src.stem}.dup{int(time.time())}{src.suffix}"
    shutil.move(str(src), str(dst))


def _maybe_checkpoint(conn: sqlite3.Connection, db_path: Path, truncate_mb: int) -> None:
    wal = _wal_path(db_path)
    wal_bytes = wal.stat().st_size if wal.exists() else 0

    # TRUNCATE keeps the -wal file from growing indefinitely and makes the main DB reflect changes.
    # If there are active readers, TRUNCATE may not fully truncate; that's fine.
    mode = "TRUNCATE" if wal_bytes >= int(truncate_mb) * 1024 * 1024 else "PASSIVE"
    try:
        conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    except Exception:
        # Never crash the ingester due to checkpoint issues.
        pass


def _slim_event_json(data_json: str) -> Tuple[str, bool]:
    """Remove large raw payloads from the stored event JSON.

    Returns (new_json, changed).
    """
    try:
        ev = json.loads(data_json)
        if not isinstance(ev, dict):
            return data_json, False

        sources = ev.get("sources")
        changed = False

        if isinstance(sources, dict):
            amber = sources.get("amber")
            if isinstance(amber, dict):
                if "raw_prices" in amber:
                    amber.pop("raw_prices", None)
                    changed = True
                if "raw_usage" in amber:
                    amber.pop("raw_usage", None)
                    changed = True

            alpha = sources.get("alpha")
            if isinstance(alpha, dict):
                if "raw" in alpha:
                    alpha.pop("raw", None)
                    changed = True

        if not changed:
            return data_json, False

        return json.dumps(ev, ensure_ascii=False, separators=(",", ":")), True
    except Exception:
        return data_json, False


def _retention_run(
    conn: sqlite3.Connection,
    *,
    full_hours: int,
    delete_after_days: int,
    slim_batch: int,
) -> None:
    """Apply retention to the events table.

    - For rows older than full_hours: strip heavy raw fields, mark slimmed=1
    - Optionally delete rows older than delete_after_days
    """
    now_ms = int(time.time() * 1000.0)

    # Slim older rows
    if full_hours > 0:
        cutoff_ms = now_ms - int(full_hours) * 3600 * 1000
        rows = conn.execute(
            """
            SELECT id, data_json
            FROM events
            WHERE ts_epoch_ms IS NOT NULL
              AND ts_epoch_ms < ?
              AND (slimmed IS NULL OR slimmed = 0)
            ORDER BY id
            LIMIT ?
            """,
            (int(cutoff_ms), int(max(1, slim_batch))),
        ).fetchall()

        if rows:
            for r in rows:
                rid = int(r["id"])
                data_json = r["data_json"]
                new_json, changed = _slim_event_json(data_json)
                if changed:
                    conn.execute(
                        "UPDATE events SET data_json = ?, slimmed = 1 WHERE id = ?",
                        (new_json, rid),
                    )
                else:
                    conn.execute("UPDATE events SET slimmed = 1 WHERE id = ?", (rid,))
            conn.commit()

    # Delete very old rows
    if delete_after_days > 0:
        del_cutoff_ms = now_ms - int(delete_after_days) * 86400 * 1000
        # Delete notes for those events too
        try:
            conn.execute(
                """
                DELETE FROM event_notes
                WHERE event_id IN (
                    SELECT event_id FROM events
                    WHERE ts_epoch_ms IS NOT NULL AND ts_epoch_ms < ?
                )
                """,
                (int(del_cutoff_ms),),
            )
        except Exception:
            pass

        conn.execute(
            "DELETE FROM events WHERE ts_epoch_ms IS NOT NULL AND ts_epoch_ms < ?",
            (int(del_cutoff_ms),),
        )
        conn.commit()


def _prune_processed_dir(processed_dir: Path, retention_days: int) -> None:
    if retention_days <= 0:
        return
    try:
        if not processed_dir.exists():
            return
        cutoff = time.time() - (int(retention_days) * 86400)
        for p in processed_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
                if st.st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _vacuum_db(db_path: Path) -> None:
    # VACUUM must run outside any open transaction.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        pass
    try:
        conn.execute("VACUUM")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest control-loop JSON events into SQLite")
    ap.add_argument("--export-dir", default=_env("EVENT_EXPORT_DIR", "export/events"))
    ap.add_argument("--processed-dir", default=_env("INGEST_PROCESSED_DIR", "export/processed"))
    ap.add_argument("--db", default=_env("INGEST_DB_PATH", "data/events.sqlite3"))
    ap.add_argument("--poll", type=float, default=float(_env("INGEST_POLL_SEC", "1")))
    ap.add_argument("--delete", action="store_true", default=_env_bool("INGEST_DELETE_AFTER_IMPORT", False))
    ap.add_argument("--vacuum", action="store_true", default=False, help="Run VACUUM on the DB and exit")
    args = ap.parse_args()

    export_dir = Path(args.export_dir).expanduser()
    processed_dir = Path(args.processed_dir).expanduser()
    db_path = Path(args.db).expanduser()

    export_dir.mkdir(parents=True, exist_ok=True)
    if args.vacuum:
        LOG.info(f"[ingest] vacuum db={db_path}")
        _vacuum_db(db_path)
        return 0
    conn = _init_db(db_path)

    ckpt_every = _env_float("INGEST_WAL_CHECKPOINT_SEC", 30.0)
    truncate_mb = _env_int("INGEST_WAL_TRUNCATE_MB", 16)
    last_ckpt = time.monotonic()

    retention_enabled = _env_bool("INGEST_RETENTION_ENABLED", True)
    retention_full_hours = _env_int("INGEST_RETENTION_FULL_HOURS", 48)
    retention_delete_after_days = _env_int("INGEST_RETENTION_DELETE_AFTER_DAYS", 30)
    retention_every_sec = _env_float("INGEST_RETENTION_EVERY_SEC", 300.0)
    retention_slim_batch = _env_int("INGEST_RETENTION_SLIM_BATCH", 500)
    processed_retention_days = _env_int("INGEST_PROCESSED_RETENTION_DAYS", 0)

    last_retention = time.monotonic()


    LOG.info(
        f"[ingest] export_dir={export_dir} processed_dir={processed_dir} db={db_path} "
        f"delete={bool(args.delete)} ckpt={ckpt_every}s truncate_mb={truncate_mb} "
        f"retention={'on' if retention_enabled else 'off'} full_h={retention_full_hours} "
        f"del_d={retention_delete_after_days} ret_every={retention_every_sec}s slim_batch={retention_slim_batch} "
        f"proc_ret_d={processed_retention_days}"
    )

    try:
        while True:
            # Only ingest stable .json files (control.py writes via atomic rename).
            paths = sorted(p for p in export_dir.glob("*.json") if p.is_file())
            if not paths:
                # Still checkpoint occasionally so the main DB stays up to date.
                now = time.monotonic()
                if ckpt_every > 0 and (now - last_ckpt) >= ckpt_every:
                    _maybe_checkpoint(conn, db_path, truncate_mb)
                    last_ckpt = now

                if retention_enabled and retention_every_sec > 0 and (now - last_retention) >= retention_every_sec:
                    try:
                        _retention_run(
                            conn,
                            full_hours=retention_full_hours,
                            delete_after_days=retention_delete_after_days,
                            slim_batch=retention_slim_batch,
                        )
                    except Exception:
                        pass
                    try:
                        _prune_processed_dir(processed_dir, processed_retention_days)
                    except Exception:
                        pass
                    last_retention = now
                time.sleep(max(0.2, float(args.poll)))
                continue

            for path in paths:
                ok = _ingest_one(conn, path)
                if ok:
                    if args.delete:
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass
                    else:
                        try:
                            _move_processed(path, processed_dir)
                        except Exception:
                            # If move fails, don't reprocess forever: rename in-place.
                            try:
                                path.rename(path.with_suffix(path.suffix + ".done"))
                            except Exception:
                                pass
                else:
                    # If it's malformed, quarantine it so we don't spin on it.
                    try:
                        _move_processed(path, processed_dir / "bad")
                    except Exception:
                        pass

            now = time.monotonic()
            if ckpt_every > 0 and (now - last_ckpt) >= ckpt_every:
                _maybe_checkpoint(conn, db_path, truncate_mb)
                last_ckpt = now

            if retention_enabled and retention_every_sec > 0 and (now - last_retention) >= retention_every_sec:
                try:
                    _retention_run(
                        conn,
                        full_hours=retention_full_hours,
                        delete_after_days=retention_delete_after_days,
                        slim_batch=retention_slim_batch,
                    )
                except Exception:
                    pass
                try:
                    _prune_processed_dir(processed_dir, processed_retention_days)
                except Exception:
                    pass
                last_retention = now

            time.sleep(max(0.1, float(args.poll)))

    except KeyboardInterrupt:
        LOG.info("[ingest] stopping")
        return 0
    finally:
        try:
            # A final truncate checkpoint so the main DB file contains all recent changes.
            _maybe_checkpoint(conn, db_path, truncate_mb=0)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
