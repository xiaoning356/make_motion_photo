#!/usr/bin/env python3
"""
Create an OPPO-compatible Android Motion Photo from a JPEG and an MP4.

Default usage:
    python make_oppo_motion_photo.py

Expected files in the current directory:
    1.jpg
    1.mp4

Output:
    1_OPPOMP.jpg

Required external tools on PATH:
    ffmpeg, ffprobe, exiftool

The script only needs the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


OPPO_USER_COMMENT = "oplus_10485792"
DEFAULT_ASPECT_TOLERANCE = 0.005  # 0.5%
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


class ScriptError(RuntimeError):
    """A user-facing error that should stop processing."""


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    display_width: int
    display_height: int
    video_codec: str
    audio_codec: str | None
    rotation: int
    duration: float | None

    @property
    def aspect_ratio(self) -> float:
        return self.display_width / self.display_height


@dataclass(frozen=True)
class CropPlan:
    width: int
    height: int
    x: int
    y: int

    @property
    def filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y},setsar=1"


def run_command(
    command: Sequence[str],
    *,
    description: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an external command and return decoded output."""
    printable = shlex.join(str(part) for part in command)
    print(f"[执行] {printable}", flush=True)
    result = subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise ScriptError(f"{description}失败。\n{details}")
    return result


def find_local_file(directory: Path, filename: str) -> Path:
    """Find a file with case-insensitive matching on case-sensitive systems."""
    exact = directory / filename
    if exact.is_file():
        return exact

    lowered = filename.lower()
    for candidate in directory.iterdir():
        if candidate.is_file() and candidate.name.lower() == lowered:
            return candidate
    raise ScriptError(f"找不到输入文件：{exact}")


def check_tools() -> dict[str, str]:
    """Verify required tools and return their resolved paths."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for tool in ("ffmpeg", "ffprobe", "exiftool"):
        path = shutil.which(tool)
        if path is None:
            missing.append(tool)
        else:
            found[tool] = path

    if missing:
        raise ScriptError(
            "缺少以下外部工具："
            + ", ".join(missing)
            + "\n请把 ffmpeg、ffprobe、exiftool 所在目录加入 PATH，"
            "或从与脚本相同的终端中运行。"
        )

    versions = {
        "ffmpeg": ["ffmpeg", "-version"],
        "ffprobe": ["ffprobe", "-version"],
        "exiftool": ["exiftool", "-ver"],
    }
    print("[环境] 已找到所需工具：")
    for tool, command in versions.items():
        result = run_command(command, description=f"读取 {tool} 版本")
        first_line = (result.stdout or result.stderr).strip().splitlines()[0]
        print(f"  - {tool}: {first_line}")
    return found


def validate_input_extensions(image_path: Path, video_path: Path) -> None:
    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ScriptError(f"图片必须是 JPEG：{image_path}")
    if video_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ScriptError(f"视频必须是 MP4 或 MOV：{video_path}")


def probe_image(path: Path) -> ImageInfo:
    result = run_command(
        [
            "exiftool",
            "-j",
            "-n",
            "-FileType",
            "-ImageWidth",
            "-ImageHeight",
            str(path),
        ],
        description="读取图片信息",
    )
    try:
        data = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise ScriptError("无法解析 ExifTool 的图片信息。") from exc

    if str(data.get("FileType", "")).upper() != "JPEG":
        raise ScriptError(f"输入不是 JPEG 图片：{path}")

    try:
        width = int(data["ImageWidth"])
        height = int(data["ImageHeight"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScriptError(f"无法读取图片宽高：{path}") from exc
    if width <= 0 or height <= 0:
        raise ScriptError(f"图片尺寸无效：{width}x{height}")
    return ImageInfo(path=path, width=width, height=height)


def probe_video(path: Path) -> VideoInfo:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        description="读取视频信息",
    )
    try:
        data = json.loads(result.stdout)
        streams: list[dict[str, Any]] = data.get("streams", [])
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ScriptError("无法解析 ffprobe 的视频信息。") from exc

    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise ScriptError(f"视频中没有视频轨道：{path}")

    try:
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScriptError(f"无法读取视频宽高：{path}") from exc

    rotation = 0
    tags = video_stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(round(float(tags["rotate"])))
        except (TypeError, ValueError):
            rotation = 0
    for side_data in video_stream.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                rotation = int(round(float(side_data["rotation"])))
            except (TypeError, ValueError):
                pass
            break

    if abs(rotation) % 180 == 90:
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height

    audio_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    duration_value = (data.get("format") or {}).get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None

    return VideoInfo(
        path=path,
        width=width,
        height=height,
        display_width=display_width,
        display_height=display_height,
        video_codec=str(video_stream.get("codec_name") or "unknown"),
        audio_codec=(
            str(audio_stream.get("codec_name") or "unknown")
            if audio_stream is not None
            else None
        ),
        rotation=rotation,
        duration=duration,
    )


def aspect_ratios_match(
    image: ImageInfo,
    video: VideoInfo,
    tolerance: float,
) -> tuple[bool, float]:
    photo_ratio = image.aspect_ratio
    video_ratio = video.aspect_ratio
    difference = abs(photo_ratio - video_ratio) / photo_ratio
    return difference <= tolerance, difference


def make_crop_plan(image: ImageInfo, video: VideoInfo) -> CropPlan:
    """Calculate a centered crop that gives the video the photo's aspect ratio."""
    source_width = video.display_width
    source_height = video.display_height
    target_ratio = image.aspect_ratio
    source_ratio = video.aspect_ratio

    if source_ratio > target_ratio:
        crop_height = source_height
        crop_width = int(round(crop_height * target_ratio))
        crop_width -= crop_width % 2
    else:
        crop_width = source_width
        crop_height = int(round(crop_width / target_ratio))
        crop_height -= crop_height % 2

    crop_width = max(2, min(source_width, crop_width))
    crop_height = max(2, min(source_height, crop_height))
    crop_x = max(0, (source_width - crop_width) // 2)
    crop_y = max(0, (source_height - crop_height) // 2)

    return CropPlan(
        width=crop_width,
        height=crop_height,
        x=crop_x,
        y=crop_y,
    )


def print_manual_crop_help(
    image: ImageInfo,
    video: VideoInfo,
    crop_plan: CropPlan,
) -> None:
    print("\n照片和视频比例不一致。")
    print(
        f"照片：{image.width}x{image.height}，比例 "
        f"{image.aspect_ratio:.6f}"
    )
    print(
        f"视频显示尺寸：{video.display_width}x{video.display_height}，比例 "
        f"{video.aspect_ratio:.6f}"
        + (f"，旋转 {video.rotation}°" if video.rotation else "")
    )
    print("\n可选的手动居中裁切命令：")
    print(
        'ffmpeg -i "<视频>" '
        f'-vf "{crop_plan.filter}" '
        '-c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p '
        '-c:a aac -b:a 192k -movflags +faststart "<裁切后视频>"'
    )
    print("\n请先保存裁切后的视频，再用视频作为输入重新运行本脚本。")


def remux_or_crop_video(
    image: ImageInfo,
    video: VideoInfo,
    crop_plan: CropPlan | None,
    output_path: Path,
) -> None:
    """
    Put audio first and video second, set the MP4 brand to mp42, and
    move the index to the beginning. Copy streams when possible.
    """
    command: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(video.path),
        "-map",
        "0:a:0?",
        "-map",
        "0:v:0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "-1",
    ]

    reencode_video = crop_plan is not None or video.video_codec not in {
        "h264",
        "hevc",
    }
    if reencode_video:
        if crop_plan is not None:
            command.extend(["-vf", crop_plan.filter])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-metadata:s:v:0",
                "rotate=0",
            ]
        )
    else:
        command.extend(["-c:v", "copy"])

    if video.audio_codec is None:
        pass
    elif video.audio_codec == "aac" and not reencode_video:
        command.extend(["-c:a", "copy"])
    elif video.audio_codec == "aac":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-b:a", "192k"])

    command.extend(
        [
            "-movflags",
            "+faststart",
            "-brand",
            "mp42",
            str(output_path),
        ]
    )
    run_command(
        command,
        description="重新封装或裁切视频",
    )

    probe = probe_video(output_path)
    if probe.audio_codec is not None:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,id,codec_type",
                "-of",
                "json",
                str(output_path),
            ],
            description="检查输出轨道顺序",
        )
        streams = json.loads(result.stdout).get("streams", [])
        ordered_types = [stream.get("codec_type") for stream in streams]
        if ordered_types[:2] != ["audio", "video"]:
            raise ScriptError(
                "输出 MP4 的轨道顺序不是“音频、视频”："
                + str(ordered_types)
            )


def build_xmp(video_size: int) -> str:
    """Build the exact OPPO-compatible XMP template used by the successful sample."""
    return f"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core Test.SNAPSHOT">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description
      rdf:about=""
      xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"
      xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
      xmlns:OpCamera="http://ns.oplus.com/photos/1.0/camera/"
      xmlns:Container="http://ns.google.com/photos/1.0/container/"
      xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
      hdrgm:Version="1.0"
      GCamera:MotionPhoto="1"
      GCamera:MotionPhotoVersion="1"
      GCamera:MotionPhotoPresentationTimestampUs="0"
      OpCamera:MotionPhotoPrimaryPresentationTimestampUs="0"
      OpCamera:MotionPhotoOwner="oplus"
      OpCamera:OLivePhotoVersion="2"
      OpCamera:VideoLength="{video_size}">
      <Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item
              Item:Mime="image/jpeg"
              Item:Semantic="Primary"
              Item:Length="0"
              Item:Padding="0"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item
              Item:Mime="video/mp4"
              Item:Semantic="MotionPhoto"
              Item:Length="{video_size}"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
"""


def prepare_image(
    image: ImageInfo,
    video_size: int,
    output_path: Path,
) -> None:
    shutil.copy2(image.path, output_path)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".xmp",
        encoding="utf-8",
        delete=False,
    ) as xmp_file:
        xmp_file.write(build_xmp(video_size))
        xmp_path = Path(xmp_file.name)

    try:
        run_command(
            [
                "exiftool",
                "-overwrite_original",
                f"-UserComment={OPPO_USER_COMMENT}",
                f"-ExifImageWidth={image.width}",
                f"-ExifImageHeight={image.height}",
                f"-XMP<={xmp_path}",
                str(output_path),
            ],
            description="写入 OPPO/Android 动态照片元数据",
        )
    finally:
        xmp_path.unlink(missing_ok=True)

    with output_path.open("rb") as prepared:
        prepared.seek(-2, os.SEEK_END)
        if prepared.read(2) != b"\xff\xd9":
            raise ScriptError("处理后的 JPEG 没有以 EOI 标记结束。")


def concatenate_files(image_path: Path, video_path: Path, output_path: Path) -> None:
    partial_path = output_path.with_name(output_path.name + ".partial")
    try:
        with partial_path.open("wb") as destination:
            for source_path in (image_path, video_path):
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
        os.replace(partial_path, output_path)
    finally:
        partial_path.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_result(
    output_path: Path,
    prepared_image_path: Path,
    video_path: Path,
    image: ImageInfo,
    display_output_path: Path | None = None,
) -> None:
    expected_size = prepared_image_path.stat().st_size + video_path.stat().st_size
    actual_size = output_path.stat().st_size
    if actual_size != expected_size:
        raise ScriptError(
            f"输出大小异常：{actual_size}，预期 {expected_size}。"
        )

    exif_result = run_command(
        [
            "exiftool",
            "-j",
            "-Validate",
            "-ImageWidth",
            "-ImageHeight",
            "-UserComment",
            "-XMP-GCamera:MotionPhoto",
            "-XMP-GCamera:MotionPhotoVersion",
            "-XMP-OpCamera:MotionPhotoOwner",
            "-XMP-OpCamera:OLivePhotoVersion",
            "-XMP-OpCamera:VideoLength",
            "-XMP-GContainer:DirectoryItemLength",
            str(output_path),
        ],
        description="校验最终文件元数据",
    )
    metadata = json.loads(exif_result.stdout)[0]
    if str(metadata.get("Validate", "")).upper() != "OK":
        raise ScriptError(
            "ExifTool 完整性校验未通过："
            + json.dumps(metadata, ensure_ascii=False)
        )
    if metadata.get("UserComment") != OPPO_USER_COMMENT:
        raise ScriptError("最终文件的 OPPO UserComment 不正确。")
    if int(metadata.get("MotionPhoto", -1)) != 1:
        raise ScriptError("最终文件缺少 GCamera:MotionPhoto=1。")
    if metadata.get("MotionPhotoOwner") != "oplus":
        raise ScriptError("最终文件缺少 OpCamera:MotionPhotoOwner=oplus。")

    expected_video_size = video_path.stat().st_size
    reported_video_size = int(metadata.get("VideoLength", -1))
    if reported_video_size != expected_video_size:
        raise ScriptError(
            "XMP VideoLength 与实际视频大小不一致："
            f"{reported_video_size} != {expected_video_size}"
        )

    extracted_path = output_path.with_name(output_path.name + ".verify.mp4")
    try:
        with extracted_path.open("wb") as extracted:
            result = subprocess.run(
                ["exiftool", "-b", "-MotionPhotoVideo", str(output_path)],
                stdout=extracted,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ScriptError(f"提取封装视频失败：{detail}")
        if extracted_path.stat().st_size != expected_video_size:
            raise ScriptError("回提取的视频长度与封装前不一致。")
        if file_sha256(extracted_path) != file_sha256(video_path):
            raise ScriptError("回提取的视频 SHA-256 与封装前不一致。")

        extracted_info = probe_video(extracted_path)
        print(
            "[校验] 回提取视频："
            f"{extracted_info.display_width}x{extracted_info.display_height}, "
            f"{extracted_info.video_codec}"
            + (f" + {extracted_info.audio_codec}" if extracted_info.audio_codec else "")
        )
    finally:
        extracted_path.unlink(missing_ok=True)

    print("\n[完成] 文件校验通过：")
    print(f"  图片：{image.width}x{image.height}")
    print(f"  视频：{video_path.stat().st_size} 字节")
    print(f"  输出：{display_output_path or output_path}")
    print(f"  大小：{actual_size} 字节")
    print(f"  SHA-256：{file_sha256(output_path)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "把 JPEG 和 MP4 合成为 Google/OPPO 兼容的动态照片。"
        )
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=Path("1.jpg"),
        help="输入 JPEG，默认：1.jpg",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=Path("1.mp4"),
        help="输入 MP4/MOV，默认：1.mp4",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出文件名，默认：<图片名>_OPPOMP.jpg",
    )
    parser.add_argument(
        "--aspect-tolerance",
        type=float,
        default=DEFAULT_ASPECT_TOLERANCE,
        help=(
            "照片与视频宽高比允许的相对误差，默认 0.005（0.5%%）。"
        ),
    )
    parser.add_argument(
        "--no-auto-crop",
        action="store_true",
        help="比例不一致时不自动裁切，只显示尺寸和建议命令。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖已经存在的输出文件。",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.aspect_tolerance < 0:
        parser.error("--aspect-tolerance 不能为负数")

    image_path = args.image.resolve()
    video_path = args.video.resolve()
    if not image_path.is_file():
        image_path = find_local_file(Path.cwd(), args.image.name).resolve()
    if not video_path.is_file():
        video_path = find_local_file(Path.cwd(), args.video.name).resolve()

    validate_input_extensions(image_path, video_path)
    output_path = (
        args.output.resolve()
        if args.output is not None
        else image_path.with_name(f"{image_path.stem}_OPPOMP.jpg")
    )
    if output_path.exists() and not args.force:
        raise ScriptError(
            f"输出文件已存在：{output_path}\n"
            "如需覆盖，请加入 --force。"
        )
    if output_path.parent.exists() is False:
        raise ScriptError(f"输出目录不存在：{output_path.parent}")
    if not re.search(r"MP\.(?:JPG|JPEG)$", output_path.name, re.IGNORECASE):
        print(
            "[警告] 输出文件名没有以 MP.jpg/MP.jpeg 结束。"
            "部分 Android 相册可能因此忽略动态视频。"
        )

    check_tools()
    image = probe_image(image_path)
    video = probe_video(video_path)

    print("\n[检查] 媒体信息：")
    print(f"  图片：{image.width}x{image.height}，比例 {image.aspect_ratio:.6f}")
    print(
        f"  视频：{video.display_width}x{video.display_height}，"
        f"比例 {video.aspect_ratio:.6f}，编码 {video.video_codec}"
        + (f"，旋转 {video.rotation}°" if video.rotation else "")
    )
    if video.audio_codec:
        print(f"  音频：{video.audio_codec}")
    else:
        print("  音频：无")
    if video.duration is not None:
        print(f"  视频时长：{video.duration:.3f} 秒")

    ratios_match, ratio_difference = aspect_ratios_match(
        image,
        video,
        args.aspect_tolerance,
    )
    crop_plan: CropPlan | None = None
    if ratios_match:
        print(
            f"[检查] 宽高比一致（差异 {ratio_difference * 100:.3f}%），"
            "将直接重封装，不重新编码画面。"
        )
    else:
        crop_plan = make_crop_plan(image, video)
        print(
            f"[检查] 宽高比不一致（差异 {ratio_difference * 100:.3f}%）。"
        )
        if args.no_auto_crop:
            print_manual_crop_help(image, video, crop_plan)
            return 2
        print(
            f"[处理] 将居中裁切视频到 {crop_plan.width}x{crop_plan.height}："
            f"x={crop_plan.x}, y={crop_plan.y}。"
            "画面边缘可能被裁掉。"
        )

    with tempfile.TemporaryDirectory(prefix="oppo-motion-photo-") as temp_dir:
        temp_root = Path(temp_dir)
        output_video = temp_root / "motion.mp4"
        prepared_image = temp_root / "motion.jpg"
        staged_output = output_path.with_name(output_path.name + ".building")

        try:
            remux_or_crop_video(
                image=image,
                video=video,
                crop_plan=crop_plan,
                output_path=output_video,
            )
            prepare_image(
                image=image,
                video_size=output_video.stat().st_size,
                output_path=prepared_image,
            )
            concatenate_files(prepared_image, output_video, staged_output)
            validate_result(
                output_path=staged_output,
                prepared_image_path=prepared_image,
                video_path=output_video,
                image=image,
                display_output_path=output_path,
            )
            os.replace(staged_output, output_path)
        finally:
            staged_output.unlink(missing_ok=True)

    print(
        "\n请将输出文件复制到手机，分别测试系统相册、Google Photos 和微信。"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as error:
        print(f"\n[错误] {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n[取消] 用户中断。", file=sys.stderr)
        raise SystemExit(130)
