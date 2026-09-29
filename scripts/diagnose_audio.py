"""One-time, read-only diagnosis for the Sep 28 analog electronics recording."""

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
    # A signed URL can expire after a full decode. Use it for one read only.
    # The transcriber reports counts only; never print the private text.
    transcriber = Transcriber()
    try:
        transcript, segments = transcriber.transcribe_url(
            vpn_url, http_headers=headers,
        )
    except Exception as exc:
        # Transcriber errors can contain a signed media URL in ffmpeg stderr.
        raise RuntimeError(f"ASR diagnostic failed: {type(exc).__name__}") from None
    print(f"Diagnostic transcript chars: {len(transcript)}", flush=True)
    print(f"Diagnostic kept segments: {len(segments)}", flush=True)


if __name__ == "__main__":
    main()
