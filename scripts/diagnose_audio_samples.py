"""At most four 20-second samples; no content, URLs or credentials are emitted."""
import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnose_playback import emit, login
from src.api.icourse import ICourseClient
from src.runtime import config
from src.ai.transcriber import Transcriber

TARGETS = [('37664', '670119'), ('38713', '666924')]
SAMPLE_OFFSET_S = 1200
SAMPLE_DURATION_S = 20
MAX_SOURCES = 4
BYTES_PER_SECOND = 16000 * 4


class ProbeDeadline(BaseException):
    pass


def expired(_signum, _frame):
    raise ProbeDeadline()


@contextlib.contextmanager
def quiet_native_output():
    """Suppress Python and native-library output, including exception context."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        with open(os.devnull, 'w') as sink:
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


def candidates(info):
    sources = info.get('video_list')
    if not isinstance(sources, dict):
        return []
    result = []
    for index, (key, value) in enumerate(sources.items(), 1):
        if not isinstance(value, dict):
            continue
        url = value.get('preview_url')
        if not isinstance(url, str) or urlsplit(url).path.lower().endswith('.mp4') is False:
            continue
        key = str(key)
        safe_key = key if re.fullmatch(r'\d{1,8}', key) else '[redacted name]'
        result.append((index, safe_key, url))
    return result[:2]


def sample(client, transcriber, course_id, sub_id, index, source_key, url, now, directory):
    result = {'course_id': course_id, 'sub_id': sub_id, 'source_entry': index,
              'source_key': source_key, 'sample_offset_s': SAMPLE_OFFSET_S,
              'requested_duration_s': SAMPLE_DURATION_S, 'audio_track_present': None,
              'pcm_duration_s': 0, 'asr_clean_char_count': None, 'asr_segment_count': None}
    path = Path(directory) / f'{sub_id}-{index}.raw'
    try:
        with quiet_native_output():
            signed = client.sign_video_url(url, now=now)
            vpn_url, headers = client.get_stream_params(signed)
            command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'info',
                       '-y', '-rw_timeout', '60000000', '-headers', headers,
                       '-ss', str(SAMPLE_OFFSET_S), '-i', vpn_url,
                       '-t', str(SAMPLE_DURATION_S), '-map', '0:a:0', '-vn',
                       '-ac', '1', '-ar', '16000', '-f', 'f32le',
                       '-fs', str(SAMPLE_DURATION_S * BYTES_PER_SECOND), str(path)]
            process = subprocess.run(command, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, timeout=60, check=False)
            stderr = process.stderr.decode(errors='replace')
            result['ffmpeg_exit_code'] = process.returncode
            if 'Audio:' in stderr:
                result['audio_track_present'] = True
            elif 'matches no streams' in stderr or 'does not contain any stream' in stderr:
                result['audio_track_present'] = False
            size = path.stat().st_size if path.exists() else 0
            result['pcm_duration_s'] = round(size / BYTES_PER_SECOND, 3)
            if size:
                result['audio_track_present'] = True
            if process.returncode != 0 or size == 0:
                result['sample_status'] = 'no_audio' if result['audio_track_present'] is False else 'extraction_failed'
                return result
            # Consume only the already-extracted short PCM. Avoid applying the
            # full-lecture duration check to an intentionally bounded sample.
            with path.open('rb') as pcm:
                transcript, segments = transcriber._consume_pcm_stream(
                    read_fn=pcm.read, is_eof_fn=lambda: True,
                    stderr_provider=lambda: b'', return_code_fn=lambda: 0,
                    timeout=60, label='bounded sample')
            result['asr_clean_char_count'] = len(transcript.strip())
            result['asr_segment_count'] = len(segments)
            result['sample_status'] = 'completed'
    except subprocess.TimeoutExpired:
        result['sample_status'] = 'read_timeout_60s'
    except Exception as exc:
        result['sample_status'] = 'failed'
        result['error_type'] = type(exc).__name__
    finally:
        path.unlink(missing_ok=True)
    return result


def main():
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(480)
    model_dir = Path('sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17')
    if not model_dir.is_dir() or not Path('silero_vad.onnx').is_file():
        emit({'probe': 'stopped', 'reason': 'existing_model_cache_unavailable'})
        return
    vpn = login()
    client = ICourseClient(vpn)
    transcriber = Transcriber(backend='sensevoice', model_dir=str(model_dir), num_threads=2)
    emitted = 0
    with tempfile.TemporaryDirectory(prefix='icourse-bounded-samples-') as directory:
        for course_id, sub_id in TARGETS:
            try:
                with quiet_native_output():
                    info = client.get_sub_info(course_id, sub_id)
                    sources = candidates(info)
                    now = info.get('now')
                    now = int(now) if now else None
                emit({'course_id': course_id, 'sub_id': sub_id,
                      'candidate_source_count': len(sources)})
                for index, key, url in sources:
                    if emitted >= MAX_SOURCES:
                        break
                    result = sample(client, transcriber, course_id, sub_id,
                                    index, key, url, now, directory)
                    emit(result)
                    emitted += 1
            except Exception as exc:
                emit({'course_id': course_id, 'sub_id': sub_id, 'error_type': type(exc).__name__})
    emit({'probe': 'completed', 'source_samples': emitted,
          'maximum_requested_pcm_seconds': MAX_SOURCES * SAMPLE_DURATION_S})
    signal.alarm(0)


if __name__ == '__main__':
    try:
        main()
    except ProbeDeadline:
        emit({'probe': 'stopped', 'reason': 'eight_minute_limit'})
        sys.exit(1)
    except Exception as exc:
        emit({'probe': 'failed', 'error_type': type(exc).__name__})
        sys.exit(1)
