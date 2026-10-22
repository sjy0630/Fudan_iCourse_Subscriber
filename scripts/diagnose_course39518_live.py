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

COURSE_ID = "39518"
TEST_DATE = "2026-10-22"


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


def summarize_access(payload):
    data = payload.get("data") or {}
    result = summarize(data)
    fields = result["fields"]
    return {
        "api_code": payload.get("code") if isinstance(payload.get("code"), int) else None,
        "message_flags": [phrase for phrase in (
            "未到开放时间", "未开始", "直播中", "直播已结束", "无权限", "没有权限",
            "未登录", "请登录", "登录失效", "未找到", "成功"
        ) if phrase in str(payload.get("msg", ""))],
        "access_fields": [item for item in fields if item["field"] in (
            "data.can_watch", "data.sub_status", "data.live_url", "data.live_url.output",
            "data.live_url.output_student", "data.playurl", "data.video_list",
            "data.content.playback", "data.content.live", "data.replay_open_at"
        )],
        "media_fields": [
            {key: item[key] for key in ("field", "scheme", "extension", "query_present")}
            for item in result["url_metadata"]
            if item["extension"] in (".mp4", ".m3u8", ".mpd", ".flv")
        ],
        "url_counts": result["url_counts"],
        "inspection_truncated": result["inspection_truncated"],
        "watch_rule": safe_rule(data.get("watch_rule")) if isinstance(data, dict) else None,
    }


def safe_rule(value):
    if not isinstance(value, str) or len(value) > 160:
        return None
    if any(marker in value.lower() for marker in ("http", "token", "@", "<", ">")):
        return "[omitted]"
    return re.sub(r"\d{4,}", "[number]", value)


def course_policy(data):
    out = {}
    for key, value in data.items():
        if not re.search(r"live|watch|public|permission|member|role|student|auth|allow|publish", key, re.I):
            continue
        if isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, int) and -1 <= value <= 100:
            out[key] = value
        elif isinstance(value, str) and re.fullmatch(r"(?:[0-9]{1,3}|yes|no|true|false)", value):
            out[key] = value
        else:
            out[key] = {"type": type(value).__name__}
    return out


def authenticated_get(client, route, params):
    for attempt in range(2):
        with quiet():
            alive = client.check_alive()
        if not alive:
            client = login()
        try:
            with quiet():
                response = client.vpn.get(client.base_url + route, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
            return client, response.status_code, payload
        except Exception:
            if attempt:
                raise
            client = login()


def main():
    import requests
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(600)
    emit({"scope_course": COURSE_ID, "scope_date": TEST_DATE,
          "time_shanghai": datetime.now(timezone(timedelta(hours=8))).isoformat()})
    client = login()
    # Match the ordinary course-detail page using only this account's own ID.
    with quiet():
        own_account = client.get_userinfo().get("account", "")
    client, status, payload = authenticated_get(client,
        "/courseapi/v3/multi-search/get-course-detail",
        {"course_id": COURSE_ID, "student": own_account})
    if payload.get("code") != 0:
        emit({"course_lookup": "denied", "http_status": status})
        return
    emit({"course_policy_fields": course_policy(payload.get("data") or {}),
          "own_account_parameter_used": bool(own_account)})
    today = []
    for months in (payload.get("data") or {}).get("sub_list", {}).values():
        for days in months.values():
            for items in days.values():
                for item in items:
                    if str(item.get("sub_title", "")).startswith(TEST_DATE) and "id" in item:
                        today.append(item)
    unique = {str(item["id"]): item for item in today}
    emit({"today_lectures": [
        {"sub_id": sid, "status": str(item.get("sub_status", "")),
         "playback_status": str(item.get("playback_status", ""))}
        for sid, item in unique.items()
    ]})
    if len(unique) > 3:
        raise RuntimeError("TooManyCurrentLectures")
    for sid in unique:
        params = {"course_id": COURSE_ID, "sub_id": sid}
        for name, route in (
            ("sub_info", "/courseapi/v3/portal-home-setting/get-sub-info"),
            ("sub_detail", "/courseapi/v3/multi-search/get-sub-detail"),
        ):
            client, status, payload = authenticated_get(client, route, params)
            emit({"sub_id": sid, "endpoint": name, "mode": "existing_account",
                  "http_status": status, **summarize_access(payload)})
            try:
                # A fresh direct request, without account cookies or bearer tokens.
                with requests.get(client.base_url + route, params=params,
                                  timeout=20, allow_redirects=False) as response:
                    report = {"sub_id": sid, "endpoint": name, "mode": "anonymous_direct",
                              "http_status": response.status_code,
                              "redirected": 300 <= response.status_code < 400}
                    try:
                        report.update(summarize_access(response.json()))
                    except ValueError:
                        report["json_response"] = False
                    emit(report)
            except Exception as exc:
                emit({"sub_id": sid, "endpoint": name, "mode": "anonymous_direct",
                      "error_type": type(exc).__name__})
    emit({"completed": True, "media_requests": 0, "database_writes": 0, "emails": 0})
    signal.alarm(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"failed": True, "error_type": type(exc).__name__})
        sys.exit(1)


