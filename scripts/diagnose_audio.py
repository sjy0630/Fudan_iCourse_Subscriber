"""One-time, read-only diagnosis for the Sep 28 analog electronics recording."""

import re
import subprocess

from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession
from src.ai.transcriber import Transcriber


COURSE_ID = "37664"
SUB_ID = "670119"


def main():
    vpn = WebVPNSession()
    vpn.login()
    vpn.authenticate_icourse()
    client = ICourseClient(vpn)

    try:
        segments = client.get_transcript_segments(SUB_ID)
        print(f"Official transcript segments: {len(segments or [])}", flush=True)
    except Exception as exc:
        print(f"Official transcript probe failed: {type(exc).__name__}", flush=True)

    url = client.get_video_url(COURSE_ID, SUB_ID)
    if not url:
        raise RuntimeError("No video URL for target lecture")
    vpn_url, headers = client.get_stream_params(url)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-headers", headers,
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", vpn_url, "-vn", "-ar", "16000", "-ac", "1",
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, timeout=900, check=False,
    )
    print(f"ffmpeg exit code: {result.returncode}", flush=True)
    for line in result.stderr.splitlines():
        if re.search(r"mean_volume:|max_volume:|Duration:|Audio:", line):
            print(line.strip(), flush=True)
    if result.returncode:
        raise RuntimeError("ffmpeg could not decode the target lecture")

    # Same mono decode, VAD, and recognizer as the production pipeline.
    # The transcriber reports counts only; never print the private text.
    transcriber = Transcriber()
    transcript, segments = transcriber.transcribe_url(
        vpn_url, http_headers=headers,
    )
    print(f"Diagnostic transcript chars: {len(transcript)}", flush=True)
    print(f"Diagnostic kept segments: {len(segments)}", flush=True)


if __name__ == "__main__":
    main()
