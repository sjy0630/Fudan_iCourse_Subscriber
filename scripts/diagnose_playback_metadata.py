"""Read-only metadata inspection. No media requests or raw URL logging."""
import contextlib
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession

TARGETS = (("38713", "687404"), ("39532", "684054"), ("39532", "688667"))


def emit(data):
    print(json.dumps(data, ensure_ascii=False, allow_nan=False), flush=True)


@contextlib.contextmanager
def quiet():
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            yield
            sys.stdout.flush()
            sys.stderr.flush()
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in saved:
            os.close(fd)


def login():
    for attempt in range(5):
        stage = "webvpn"
        try:
            with quiet():
                vpn = WebVPNSession()
                vpn.login()
                stage = "icourse"
                vpn.authenticate_icourse()
                client = ICourseClient(vpn)
                stage = "verify"
                if not client.check_alive():
                    raise RuntimeError("SessionNotVerified")
            emit({"login": "verified"})
            return client
        except Exception as exc:
            emit({"login_error_type": type(exc).__name__, "stage": stage,
                  "reason_flags": [phrase for phrase in (
                      "Failed to extract lck", "Authentication failed", "SessionNotVerified",
                      "iCourse CAS auth failed", "Failed to get authChainCode",
                      "Failed to get public key", "No loginToken", "Failed to extract ticket",
                      "Failed to establish WebVPN session") if phrase in str(exc)],
                  "codes": re.findall(r"(?:code|status)=(\d{3,6})", str(exc))})
            time.sleep(5)
    raise RuntimeError("LoginFailed")


def safe_key(key):
    key = str(key)
    return key if re.fullmatch(r"[\w.-]{1,64}", key) else "[non-field key]"


def summarize(payload):
    """Output field types/lengths and sanitized URL metadata, never bodies."""
    rows, urls = [], []
    truncated = False

    def walk(value, path, depth):
        nonlocal truncated
        if len(rows) >= 350 or depth > 7:
            truncated = True
            return
        row = {"field": path, "type": type(value).__name__}
        if isinstance(value, dict):
            row.update(count=len(value), keys=[safe_key(k) for k in value])
            rows.append(row)
            for key, item in value.items():
                walk(item, path + "." + safe_key(key), depth + 1)
        elif isinstance(value, list):
            row["count"] = len(value)
            truncated = truncated or len(value) > 6
            rows.append(row)
            for index, item in enumerate(value[:6]):
                walk(item, f"{path}[{index}]", depth + 1)
        elif isinstance(value, str):
            row["length"] = len(value)
            if re.search(r"(?:status|code)$", path, re.I) and re.fullmatch(r"\d{1,6}", value):
                row["value"] = int(value)
            rows.append(row)
            candidates = re.findall(r"https?://[^\s\"'<>]+", value.replace("\\/", "/"))
            if value.startswith("/") and not candidates:
                candidates = [value]
            for candidate in candidates[:8]:
                parsed = urlsplit(candidate)
                extension = Path(parsed.path).suffix.lower()
                urls.append({"field": path, "scheme": parsed.scheme,
                             "host": parsed.hostname, "extension": extension if re.fullmatch(r"\.[a-z0-9]{1,8}", extension) else "",
                             "length": len(candidate), "query_present": bool(parsed.query),
                             "query_length": len(parsed.query)})
        else:
            if value is None or isinstance(value, bool):
                row["value"] = value
            elif re.search(r"(?:status|code|duration|length|count)$", path, re.I):
                row["value"] = value
            rows.append(row)

    walk(payload, "data", 0)
    return {"fields": rows, "url_metadata": urls,
            "url_counts": {kind: sum(item["extension"] == kind for item in urls)
                           for kind in (".mp4", ".m3u8", ".mpd", ".flv")},
            "inspection_truncated": truncated}


def deadline(*_args):
    raise TimeoutError("MetadataDeadline")


def main():
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(600)
    client = login()
    for COURSE_ID, SUB_ID in TARGETS:
        for name, route in (
            ("sub_info", "/courseapi/v3/portal-home-setting/get-sub-info"),
            ("sub_detail", "/courseapi/v3/multi-search/get-sub-detail"),
        ):
            try:
                for attempt in range(2):
                    with quiet():
                        alive = client.check_alive()
                    if not alive:
                        client = login()
                    try:
                        with quiet():
                            response = client.vpn.get(client.base_url + route,
                                params={"course_id": COURSE_ID, "sub_id": SUB_ID})
                            response.raise_for_status()
                            payload = response.json()
                        break
                    except Exception:
                        if attempt:
                            raise
                        emit({"course_id": COURSE_ID, "sub_id": SUB_ID,
                              "endpoint": name, "retry_with_fresh_session": True})
                        client = login()
                report = {"course_id": COURSE_ID, "sub_id": SUB_ID, "endpoint": name, "http_status": response.status_code,
                          "response_keys": [safe_key(k) for k in payload],
                          "api_code": payload.get("code") if isinstance(payload.get("code"), (int, float)) else None,
                          "message_flags": [phrase for phrase in ("未到开放时间", "暂无", "未找到", "成功")
                                            if phrase in str(payload.get("msg", ""))]}
                details = payload.get("data") or {}
                open_at = details.get("replay_open_at")
                if isinstance(open_at, (int, float)) and open_at > 0:
                    try:
                        seconds = open_at / 1000 if open_at > 100000000000 else open_at
                        report["replay_open_at_shanghai"] = datetime.fromtimestamp(
                            seconds, timezone(timedelta(hours=8))).isoformat()
                    except (ValueError, OverflowError, OSError):
                        report["replay_open_at_valid"] = False
                report.update(summarize(payload.get("data")))
                emit(report)
            except Exception as exc:
                emit({"course_id": COURSE_ID, "sub_id": SUB_ID, "endpoint": name, "error_type": type(exc).__name__})
    emit({"completed": True, "media_requests": 0, "database_writes": 0, "emails": 0})
    signal.alarm(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"failed": True, "error_type": type(exc).__name__})
        sys.exit(1)

