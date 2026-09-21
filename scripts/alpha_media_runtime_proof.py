from __future__ import annotations

import asyncio
import subprocess
import tempfile
import wave
from pathlib import Path

from recantor.media_processing import MediaPermanentError, probe_media


def run(*argv: str) -> None:
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def make_source_wav(path: Path) -> None:
    run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:duration=0.8:sample_rate=48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(path),
    )


def convert(source: Path, target: Path, *codec_args: str) -> None:
    run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        *codec_args,
        str(target),
    )


def normalize(source: Path, target: Path, stream_index: int) -> None:
    run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-copyts",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-af",
        "aresample=16000:async=1:first_pts=0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-t",
        "2.000",
        "-f",
        "wav",
        str(target),
    )


def assert_normalized_wav(path: Path) -> None:
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnframes() > 0


async def expect_probe_error(path: Path, code: str) -> None:
    try:
        await probe_media(path)
    except MediaPermanentError as exc:
        assert exc.code == code, (path.name, exc.code, code)
    else:
        raise AssertionError(f"{path.name} unexpectedly passed probe")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="recantor-media-proof-") as temp:
        root = Path(temp)
        wav = root / "fixture.wav"
        make_source_wav(wav)

        fixtures = [
            wav,
            root / "fixture.mp3",
            root / "fixture.m4a",
            root / "fixture.ogg",
            root / "fixture.webm",
            root / "fixture.mp4",
        ]
        convert(wav, fixtures[1], "-c:a", "libmp3lame")
        convert(wav, fixtures[2], "-c:a", "aac")
        convert(wav, fixtures[3], "-c:a", "libvorbis")
        convert(wav, fixtures[4], "-c:a", "libopus")
        convert(wav, fixtures[5], "-c:a", "aac")

        for index, fixture in enumerate(fixtures):
            probe = await probe_media(fixture)
            normalized = root / f"normalized-{index}.wav"
            normalize(fixture, normalized, probe.audio_stream_index)
            assert_normalized_wav(normalized)

        corrupt = root / "corrupt.bin"
        corrupt.write_bytes(b"not-media")
        await expect_probe_error(corrupt, "corrupt_media")

        no_audio = root / "no-audio.mp4"
        run(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.5",
            "-an",
            "-c:v",
            "mpeg4",
            str(no_audio),
        )
        await expect_probe_error(no_audio, "no_audio")

    print("MEDIA_RUNTIME_PROOF_PASS supported=6 normalized=6 negative=2")


if __name__ == "__main__":
    asyncio.run(main())
