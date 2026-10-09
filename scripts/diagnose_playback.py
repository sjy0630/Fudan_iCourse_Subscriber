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


def ppt_duration_hint(vpn, course_id, sub_id):
    """Read screenshot metadata only; never fetch image URLs."""
    hint = 0
    count = 0
    for page in range(1, 1001):
        metadata, data = read_json(vpn, '/pptnote/v1/schedule/search-ppt',
                                   {'course_id': course_id, 'sub_id': sub_id,
                                    'page': page, 'per_page': 100})
        if data is None or data.get('code') != 0:
            return {'ppt_metadata_ok': False, 'ppt_failure': metadata,
                    'ppt_item_count': count, 'ppt_max_offset_s': hint}
        rows = data.get('list', [])
        if not isinstance(rows, list):
            return {'ppt_metadata_ok': False, 'ppt_item_count': count,
                    'ppt_max_offset_s': hint}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                content = json.loads(row.get('content', '{}'))
                if not isinstance(content, dict) or not content.get('pptimgurl'):
                    continue
                offset = int(row.get('created_sec', 0) or 0)
            except (ValueError, TypeError):
                continue
            hint = max(hint, offset)
            count += 1
        if len(rows) < 100:
            return {'ppt_metadata_ok': True, 'ppt_item_count': count,
                    'ppt_max_offset_s': hint}
    return {'ppt_metadata_ok': False, 'ppt_item_count': count,
            'ppt_max_offset_s': hint, 'ppt_page_limit_reached': True}


def transcript_timing(content, duration_hint_s):
    """Replicate the runner's 20-minute head/gap/tail completeness rule."""
    segments = []
    for row in content if isinstance(content, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('Text'), str) or not row['Text'].strip():
            continue
        try:
            start = int(row.get('BeginSec', 0)) * 1000
            end = int(row.get('EndSec', row.get('BeginSec', 0))) * 1000
        except (ValueError, TypeError):
            return {'timing_parse_ok': False, 'usable_by_20min_rule': False}
        segments.append((start, end))
    segments.sort(key=lambda value: value[0])
    if not segments:
        return {'timing_parse_ok': True, 'nonempty_segment_count': 0,
                'usable_by_20min_rule': False}
    head_gap = segments[0][0]
    prev_end = segments[0][1]
    max_gap = 0
    for start, end in segments[1:]:
        max_gap = max(max_gap, start - prev_end)
        prev_end = max(prev_end, end)
    tail_gap = max(0, duration_hint_s * 1000 - prev_end) if duration_hint_s else 0
    usable = head_gap <= 1200000 and max_gap <= 1200000 and tail_gap <= 1200000
    return {'timing_parse_ok': True, 'nonempty_segment_count': len(segments),
            'first_start_min': round(head_gap / 60000, 3),
            'last_end_min': round(prev_end / 60000, 3),
            'max_internal_gap_min': round(max_gap / 60000, 3),
            'tail_gap_to_ppt_min': round(tail_gap / 60000, 3),
            'usable_by_20min_rule': usable}


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
        ppt = ppt_duration_hint(vpn, course_id, sub_id)
        timing = transcript_timing(content, ppt['ppt_max_offset_s']) if count is not None else {
            'usable_by_20min_rule': False, 'transcript_metadata_ok': False}
        if not ppt['ppt_metadata_ok']:
            timing['usable_by_20min_rule'] = None
        emit({'course_id': course_id, 'sub_id': sub_id, 'endpoint': 'transcript-completeness',
              **ppt, **timing})


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit({'probe': 'failed', 'error_type': type(exc).__name__})
        sys.exit(1)
