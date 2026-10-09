"""Read-only metadata probe; output excludes URLs, credentials and content."""
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.api.webvpn import WebVPNSession
from src.runtime import config

TARGETS = [('37664', '670119'), ('39518', '678137'),
           ('40322', '677927'), ('39532', '678206')]
SAFE_MESSAGES = {'success', 'Success', 'ok', 'OK', '成功', '操作成功',
                 '请求成功', '视频未到开放时间', '视频未到开放时间！',
                 '视频未到开放时间!', '未登录', '登录已过期', '登录失效',
                 '请先登录', '暂无数据', '没有权限', '无权限', ''}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def safe_message(value):
    if value is None:
        return None
    return value if isinstance(value, str) and value in SAFE_MESSAGES else '[redacted unrecognized message]'


def safe_code(value):
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) < 1000000:
        return value
    if isinstance(value, str) and re.fullmatch(r'-?\d{1,6}', value):
        return value
    return None


def safe_keys(value):
    if not isinstance(value, dict):
        return []
    return sorted(key if isinstance(key, str)
                  and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,40}', key)
                  and not re.fullmatch(r'[a-fA-F0-9]{16,}', key)
                  else '[redacted key]' for key in value)


def media_presence(value):
    flags = {'has_mp4_path': False, 'has_m3u8_path': False}
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str):
            for extension in ('mp4', 'm3u8'):
                if re.search(r'\.' + extension + r'(?:[?&#\s"\']|$)', current, re.I):
                    flags[f'has_{extension}_path'] = True
    return flags


def read_json(vpn, path, params):
    # Suppress existing helpers' output and exception text: both may contain
    # signed redirect URLs. Emit only status, allowlisted messages and types.
    with open(os.devnull, 'w') as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                response = vpn.get(config.ICOURSE_BASE + path, params=params, timeout=30)
                status = response.status_code
                try:
                    data = response.json()
                except ValueError:
                    return {'http_status': status, 'json_object': False}, None
                if not isinstance(data, dict):
                    return {'http_status': status, 'json_object': False}, None
                return {'http_status': status, 'api_code': safe_code(data.get('code')),
                        'api_message': safe_message(data.get('msg', data.get('message')))}, data
            except Exception as exc:
                return {'error_type': type(exc).__name__}, None


def login():
    for attempt in range(1, 4):
        with open(os.devnull, 'w') as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                try:
                    vpn = WebVPNSession()
                    vpn.login()
                    vpn.authenticate_icourse()
                except Exception as exc:
                    failure = type(exc).__name__
                else:
                    failure = None
        if failure is None:
            metadata, data = read_json(vpn, '/userapi/v1/infosimple', {})
            if metadata.get('http_status') == 200 and metadata.get('api_code') in (0, 200):
                emit({'login': 'verified', 'attempt': attempt})
                return vpn
            failure = 'SessionVerificationFailed'
        emit({'login': 'failed', 'attempt': attempt, 'error_type': failure})
        if attempt < 3:
            time.sleep(5)
    raise RuntimeError('AuthenticationFailed')


def main():
    vpn = login()
    for course_id, sub_id in TARGETS:
        for endpoint, path in (
            ('get-sub-info', '/courseapi/v3/portal-home-setting/get-sub-info'),
            ('get-sub-detail', '/courseapi/v3/multi-search/get-sub-detail'),
        ):
            metadata, data = read_json(vpn, path, {'course_id': course_id, 'sub_id': sub_id})
            result = {'course_id': course_id, 'sub_id': sub_id, 'endpoint': endpoint, **metadata}
            if data is not None:
                payload = data.get('data')
                video_list = payload.get('video_list') if isinstance(payload, dict) else None
                result.update(payload_type=type(payload).__name__, payload_keys=safe_keys(payload),
                              video_list_type=type(video_list).__name__,
                              video_list_count=len(video_list) if isinstance(video_list, (dict, list)) else 0,
                              **media_presence(payload))
            emit(result)
        metadata, data = read_json(vpn, '/courseapi/v3/web-socket/search-trans-result',
                                   {'sub_id': sub_id, 'format': 'json'})
        count = None
        if data is not None and data.get('code') == 0:
            rows = data.get('list')
            first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            content = first.get('all_content')
            count = sum(1 for row in content if isinstance(row, dict)
                        and isinstance(row.get('Text'), str) and row['Text'].strip()) if isinstance(content, list) else 0
        emit({'course_id': course_id, 'sub_id': sub_id, 'endpoint': 'official-transcript',
              **metadata, 'segment_count': count})


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit({'probe': 'failed', 'error_type': type(exc).__name__})
        sys.exit(1)
