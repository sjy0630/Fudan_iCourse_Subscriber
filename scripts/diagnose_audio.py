"""One-time, read-only diagnosis for the Sep 28 analog electronics recording."""

import time

from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession
from src.ai.transcriber import IncompleteAudioError, Transcriber


COURSE_ID = "37664"
SUB_ID = "670119"


def main():
    # Match the production runner's resilience to a cold WebVPN session.
    for attempt in range(5):
        try:
            vpn = WebVPNSession()
            vpn.login()
            vpn.authenticate_icourse()
            break
        except Exception as exc:
            print(f"VPN login attempt {attempt + 1}: {type(exc).__name__}")
            if attempt == 4:
                raise RuntimeError("VPN authentication failed") from None
            time.sleep(5)
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
    # Test whether the production VAD threshold misses quiet speech. Limit
    # this probe to the first 30 minutes; the signed URL is used once only.
    # The transcriber reports counts only; never print the private text.
    transcriber = Transcriber()
    transcriber._init()
    transcriber._vad_config.silero_vad.threshold = 0.1
    cmd = [
        "ffmpeg", "-nostdin", "-headers", headers,
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5", "-i", vpn_url,
        "-t", "1800", "-vn", "-ar", "16000", "-ac", "1",
        "-f", "f32le", "-",
    ]
    try:
        transcript, segments = transcriber._transcribe_with_inline_ffmpeg(cmd)
    except IncompleteAudioError as exc:
        # Expected because this diagnostic intentionally samples 30 minutes.
        transcript, segments = exc.transcript, exc.segments
    except Exception as exc:
        # Transcriber errors can contain a signed media URL in ffmpeg stderr.
        raise RuntimeError(f"ASR diagnostic failed: {type(exc).__name__}") from None
    print(f"Diagnostic transcript chars: {len(transcript)}", flush=True)
    print(f"Diagnostic kept segments: {len(segments)}", flush=True)


if __name__ == "__main__":
    main()
