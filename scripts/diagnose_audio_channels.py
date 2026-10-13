"""Approved one-off probe: four lectures, two sources each, 30 seconds/source.

Keep native channels to compare with ffmpeg's production mono downmix. Emit
statistics only; never upload media, transcripts, signed URLs or credentials.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession
from src.ai.transcriber import Transcriber, _postprocess_segment

TARGETS = [("37664", "670119"), ("37664", "680154"),
           ("38713", "666924"), ("38713", "756197")]
OFFSET = 600
DURATION = 30


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


def candidates(info, detail):
    urls = []
    for value in (info.get("video_list") or {}).values():
        if isinstance(value, dict):
            urls.append(value.get("preview_url"))
    urls.extend((info.get("playurl") or {}).values())
    for payload in (info, detail):
        urls.append(((payload.get("content") or {}).get("playback") or {}).get("url"))
    result = []
    for url in urls:
        if isinstance(url, str) and urlsplit(url).path.endswith(".mp4") and url not in result:
            result.append(url)
    return result[:2]


def rms(samples):
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def summarize_audio(transcriber, samples):
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    stream = transcriber._recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    transcriber._recognizer.decode_stream(stream)
    raw_text = stream.result.text
    clean = _postprocess_segment(raw_text)
    pcm = io.BytesIO(samples.tobytes())
    text, segments = transcriber._consume_pcm_stream(
        pcm.read, lambda: True, lambda: b"", lambda: 0,
        timeout=90, label="local channel comparison")
    return {"rms": round(rms(samples), 8),
            "peak": round(float(np.max(np.abs(samples))), 8),
            "raw_chars": len(raw_text),
            "raw_han_chars": len(re.findall(r"[\u4e00-\u9fff]", raw_text)),
            "clean_chars": len(clean), "pipeline_chars": len(text),
            "pipeline_segments": len(segments)}


def probe(client, transcriber, source, now, directory):
    wav = Path(directory) / "sample.wav"
    try:
        signed = client.sign_video_url(source, now=now)
        url, headers = client.get_stream_params(signed)
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-rw_timeout", "45000000",
                   "-headers", headers, "-ss", str(OFFSET), "-i", url,
                   "-t", str(DURATION), "-vn", "-ar", "16000",
                   "-c:a", "pcm_f32le", "-fs", "32000000", str(wav)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=90)
        stderr = result.stderr.decode(errors="replace")
        input_log = stderr.split("Output #0", 1)[0]
        info = {"ffmpeg_exit": result.returncode,
                "input_audio_streams": len(re.findall(r"Stream #0:\d+.*Audio:", input_log)),
                "selected_stream_indices": re.findall(r"Stream #0:(\d+) -> #0:\d+", stderr),
                "error_flags": [m for m in ("Invalid data found", "moov atom not found",
                    "403 Forbidden", "404 Not Found", "matches no streams", "Connection timed out")
                    if m.lower() in stderr.lower()]}
        if result.returncode != 0 or not wav.is_file():
            return info
        meta = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate", "-of", "json", str(wav)],
            capture_output=True, timeout=10, check=True)
        audio = json.loads(meta.stdout)["streams"][0]
        channels = int(audio["channels"])
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=10, check=True).stdout
        data = np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)
        info.update(channels=channels, decoded_seconds=len(data) / 16000)
        if not len(data):
            return info
        # Reuse the same local sample; this does not fetch any more remote audio.
        mono = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1",
            "-f", "f32le", "pipe:1"], capture_output=True, timeout=10, check=True).stdout
        mono = np.frombuffer(mono, dtype=np.float32)
        info["production_mono"] = summarize_audio(transcriber, mono)
        for i in range(min(channels, 2)):
            info[f"channel_{i}"] = summarize_audio(transcriber, data[:, i])
        if channels == 2:
            denom = np.linalg.norm(data[:, 0].astype(float)) * np.linalg.norm(data[:, 1].astype(float))
            corr = np.dot(data[:, 0].astype(float), data[:, 1].astype(float)) / denom if denom else 0
            info["channel_correlation"] = round(float(corr), 6)
            info["mono_to_strongest_rms_ratio"] = round(rms(mono) / max(rms(data[:, 0]), rms(data[:, 1]), 1e-12), 6)
        return info
    finally:
        wav.unlink(missing_ok=True)


def deadline(signum, frame):
    raise TimeoutError("ProbeDeadline")


def main():
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(900)
    client = login()
    transcriber = Transcriber(backend="sensevoice", num_threads=2)
    with quiet():
        transcriber._init()
    attempts = 0
    for course_id, sub_id in TARGETS:
        try:
            with quiet():
                info = client.get_sub_info(course_id, sub_id)
                try:
                    detail = client.get_sub_detail(course_id, sub_id)
                except Exception:
                    detail = {}
                sources = candidates(info, detail)
                now = int(info["now"]) if info.get("now") else None
            emit({"course_id": course_id, "sub_id": sub_id, "sources": len(sources)})
            for index, source in enumerate(sources):
                attempts += 1
                report = {"course_id": course_id, "sub_id": sub_id, "source_index": index,
                          "offset_s": OFFSET, "requested_seconds": DURATION}
                try:
                    with tempfile.TemporaryDirectory() as directory, quiet():
                        report.update(probe(client, transcriber, source, now, directory))
                except Exception as exc:
                    report["error_type"] = type(exc).__name__
                emit(report)
        except Exception as exc:
            emit({"course_id": course_id, "sub_id": sub_id, "error_type": type(exc).__name__})
    emit({"completed": True, "sample_attempts": attempts, "maximum_requested_seconds": attempts * DURATION})
    signal.alarm(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"failed": True, "error_type": type(exc).__name__})
        sys.exit(1)
