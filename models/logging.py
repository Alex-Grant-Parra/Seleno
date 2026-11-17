import json
import logging
from datetime import datetime
from pathlib import Path

from app.db import db


logger = logging.getLogger(__name__)


class RequestLog(db.Model):
    __tablename__ = "request_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    request_timestamp = db.Column(db.String(64), nullable=False)
    client_ip = db.Column(db.String(64), nullable=False, index=True)
    method = db.Column(db.String(16), nullable=False, index=True)
    path = db.Column(db.String(2048), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    query_string = db.Column(db.Text, nullable=True)
    remote_addr = db.Column(db.String(64), nullable=True)
    scheme = db.Column(db.String(16), nullable=True)
    headers_json = db.Column(db.Text, nullable=True)

    @classmethod
    def from_dict(cls, request_data):
        headers = request_data.get("headers", {})
        return cls(
            request_timestamp=request_data.get("timestamp") or datetime.utcnow().isoformat(),
            client_ip=request_data.get("client_ip", "unknown"),
            method=request_data.get("method", "UNKNOWN"),
            path=request_data.get("path", ""),
            url=request_data.get("url", ""),
            query_string=request_data.get("query_string", ""),
            remote_addr=request_data.get("remote_addr"),
            scheme=request_data.get("scheme"),
            headers_json=json.dumps(headers),
        )

    @classmethod
    def save_request(cls, request_data):
        try:
            row = cls.from_dict(request_data)
            db.session.add(row)
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to save request log row: %s", exc)
            return False

    def to_log_line(self):
        payload = {
            "timestamp": self.request_timestamp,
            "client_ip": self.client_ip,
            "method": self.method,
            "path": self.path,
            "url": self.url,
            "query_string": self.query_string or "",
            "remote_addr": self.remote_addr,
            "scheme": self.scheme,
            "headers": {},
        }

        if self.headers_json:
            try:
                payload["headers"] = json.loads(self.headers_json)
            except Exception:
                payload["headers"] = {}

        return f"{self.created_at.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {json.dumps(payload)}\\n"

    @classmethod
    def import_legacy_file_if_empty(cls, log_path):
        try:
            if cls.query.first() is not None:
                return {"imported": 0, "skipped": "table_not_empty"}

            path = Path(log_path)
            if not path.exists():
                return {"imported": 0, "skipped": "file_missing"}

            imported = 0
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if " - " not in line:
                        continue

                    prefix, payload_raw = line.split(" - ", 1)
                    payload_raw = payload_raw.strip()

                    try:
                        payload = json.loads(payload_raw)
                    except Exception:
                        continue

                    row = cls.from_dict(payload)

                    try:
                        row.created_at = datetime.strptime(prefix.strip(), "%Y-%m-%d %H:%M:%S,%f")
                    except Exception:
                        pass

                    db.session.add(row)
                    imported += 1

                    if imported % 500 == 0:
                        db.session.commit()

            db.session.commit()
            return {"imported": imported, "skipped": None}
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to import legacy request log file: %s", exc)
            return {"imported": 0, "error": str(exc)}


class SecurityLog(db.Model):
    __tablename__ = "security_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_timestamp = db.Column(db.String(64), nullable=True)
    level = db.Column(db.String(16), nullable=False, default="INFO")
    event_type = db.Column(db.String(128), nullable=True, index=True)
    client_ip = db.Column(db.String(64), nullable=True, index=True)
    method = db.Column(db.String(16), nullable=True)
    path = db.Column(db.String(2048), nullable=True)
    url = db.Column(db.Text, nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    payload_json = db.Column(db.Text, nullable=True)
    message = db.Column(db.Text, nullable=True)

    @classmethod
    def save_event(cls, payload, level="INFO", message=None):
        try:
            details = payload.get("details", {}) if isinstance(payload, dict) else {}
            row = cls(
                event_timestamp=(payload.get("timestamp") if isinstance(payload, dict) else None),
                level=(level or "INFO"),
                event_type=(payload.get("event_type") if isinstance(payload, dict) else None),
                client_ip=(payload.get("client_ip") if isinstance(payload, dict) else None),
                method=(payload.get("method") if isinstance(payload, dict) else None),
                path=(payload.get("path") if isinstance(payload, dict) else None),
                url=(payload.get("url") if isinstance(payload, dict) else None),
                user_agent=(payload.get("user_agent") if isinstance(payload, dict) else None),
                details_json=json.dumps(details) if isinstance(details, dict) else None,
                payload_json=json.dumps(payload) if isinstance(payload, dict) else None,
                message=message,
            )
            db.session.add(row)
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to save security log row: %s", exc)
            return False

    def to_log_line(self):
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
                msg = json.dumps(payload)
            except Exception:
                msg = self.message or ""
        else:
            msg = self.message or ""

        return f"{self.created_at.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {self.level} - {msg}\\n"

    @classmethod
    def import_legacy_file_if_empty(cls, log_path):
        try:
            if cls.query.first() is not None:
                return {"imported": 0, "skipped": "table_not_empty"}

            path = Path(log_path)
            if not path.exists():
                return {"imported": 0, "skipped": "file_missing"}

            imported = 0
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue

                    ts = None
                    level = "INFO"
                    msg = line

                    parts = line.split(" - ", 2)
                    if len(parts) == 3:
                        ts, level, msg = parts
                    elif len(parts) == 2:
                        ts, msg = parts

                    payload = None
                    try:
                        payload = json.loads(msg)
                    except Exception:
                        payload = None

                    row = cls(
                        level=level or "INFO",
                        message=msg,
                        payload_json=(json.dumps(payload) if isinstance(payload, dict) else None),
                        event_timestamp=(payload.get("timestamp") if isinstance(payload, dict) else None),
                        event_type=(payload.get("event_type") if isinstance(payload, dict) else None),
                        client_ip=(payload.get("client_ip") if isinstance(payload, dict) else None),
                        method=(payload.get("method") if isinstance(payload, dict) else None),
                        path=(payload.get("path") if isinstance(payload, dict) else None),
                        url=(payload.get("url") if isinstance(payload, dict) else None),
                        user_agent=(payload.get("user_agent") if isinstance(payload, dict) else None),
                        details_json=(json.dumps(payload.get("details", {})) if isinstance(payload, dict) else None),
                    )

                    if ts:
                        try:
                            row.created_at = datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S,%f")
                        except Exception:
                            pass

                    db.session.add(row)
                    imported += 1

                    if imported % 500 == 0:
                        db.session.commit()

            db.session.commit()
            return {"imported": imported, "skipped": None}
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to import legacy security.log: %s", exc)
            return {"imported": 0, "error": str(exc)}


class WebsocketSecurityLog(db.Model):
    __tablename__ = "websocket_security_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    level = db.Column(db.String(16), nullable=False, default="WARNING")
    event = db.Column(db.String(128), nullable=True, index=True)
    client_ip = db.Column(db.String(64), nullable=True, index=True)
    client_id = db.Column(db.String(255), nullable=True, index=True)
    payload_json = db.Column(db.Text, nullable=True)

    @classmethod
    def save_event(cls, event, payload=None, level="WARNING"):
        try:
            payload = payload or {}
            row = cls(
                level=level or "WARNING",
                event=event,
                client_ip=payload.get("ip"),
                client_id=payload.get("client_id"),
                payload_json=json.dumps({"event": event, **payload}),
            )
            db.session.add(row)
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to save websocket security log row: %s", exc)
            return False

    def to_log_line(self):
        payload = {}
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
            except Exception:
                payload = {"event": self.event}

        return f"{self.created_at.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {self.level} - {json.dumps(payload)}\\n"

    @classmethod
    def import_legacy_file_if_empty(cls, log_path):
        try:
            if cls.query.first() is not None:
                return {"imported": 0, "skipped": "table_not_empty"}

            path = Path(log_path)
            if not path.exists():
                return {"imported": 0, "skipped": "file_missing"}

            imported = 0
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue

                    ts = None
                    level = "WARNING"
                    msg = line

                    parts = line.split(" - ", 2)
                    if len(parts) == 3:
                        ts, level, msg = parts
                    elif len(parts) == 2:
                        ts, msg = parts

                    payload = None
                    try:
                        payload = json.loads(msg)
                    except Exception:
                        payload = None

                    row = cls(
                        level=level or "WARNING",
                        event=(payload.get("event") if isinstance(payload, dict) else None),
                        client_ip=(payload.get("ip") if isinstance(payload, dict) else None),
                        client_id=(payload.get("client_id") if isinstance(payload, dict) else None),
                        payload_json=(json.dumps(payload) if isinstance(payload, dict) else json.dumps({"message": msg})),
                    )

                    if ts:
                        try:
                            row.created_at = datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S,%f")
                        except Exception:
                            pass

                    db.session.add(row)
                    imported += 1

                    if imported % 500 == 0:
                        db.session.commit()

            db.session.commit()
            return {"imported": imported, "skipped": None}
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to import legacy websocket_security.log: %s", exc)
            return {"imported": 0, "error": str(exc)}


# --- Retention ------------------------------------------------------------
# Request and security logs grow by every request. LOG_RETENTION_DAYS (in
# instance/.env) deletes rows older than that once a day; unset or 0 keeps
# everything. Deleted rows are gone for good, so it is off unless asked for.

def prune_logs(days, batch=5000):
    """Delete log rows older than `days` days, in batches so the database is
    never locked for long. Returns {table: rows deleted}."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    removed = {}
    for model in (RequestLog, SecurityLog, WebsocketSecurityLog):
        total = 0
        while True:
            ids = [row.id for row in db.session.query(model.id).filter(model.created_at < cutoff).limit(batch)]
            if not ids:
                break
            db.session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
            db.session.commit()
            total += len(ids)
        removed[model.__tablename__] = total
    return removed


def start_log_retention(app, interval_s=24 * 3600):
    import os
    import threading
    import time
    try:
        days = int(os.getenv("LOG_RETENTION_DAYS", "0") or 0)
    except ValueError:
        days = 0
    if days <= 0:
        return None

    def run():
        time.sleep(120)
        while True:
            try:
                with app.app_context():
                    removed = prune_logs(days)
                if any(removed.values()):
                    logger.info("Log retention (%s days): removed %s", days, removed)
            except Exception:
                logger.exception("Log retention failed")
                try:
                    db.session.rollback()
                except Exception:
                    pass
            time.sleep(interval_s)

    thread = threading.Thread(target=run, name="log-retention", daemon=True)
    thread.start()
    return thread
