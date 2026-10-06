from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image
import yt_dlp


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_ZIP_SIZE = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_UNCOMPRESSED_ZIP_SIZE = 200 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 100
MAX_EMOJI_BYTES = 256 * 1024
MAX_CONCURRENT_CDN_DOWNLOADS = 8
UPLOAD_DELAY_SECONDS = 0.1
MAX_EMOJI_NAME_LENGTH = 32
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
REPORT_EMOJI_ASSETS = {
    "success": ("report_success_gif", "status_success.webp"),
    "duplicate": ("report_duplicate_gif", "status_duplicate.webp"),
    "error": ("report_error_gif", "status_error.webp"),
    "added": ("report_added_gif", "status_added.webp"),
}
REPORT_EMOJI_FALLBACKS = {
    "success": "✅",
    "duplicate": "⚠️",
    "error": "❌",
    "added": "✨",
}
REPORT_EMOJI_CACHE: dict[int, dict[str, str]] = {}


def normalize_emoji_prefix(prefix: str) -> str:
    """Normalize and validate the user-provided emoji name prefix."""
    normalized = re.sub(r"[^a-z0-9_]+", "_", prefix.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")

    if not normalized:
        raise ValueError(
            "Tiền tố tên emoji không hợp lệ. Hãy dùng chữ cái, số hoặc dấu gạch dưới."
        )

    max_prefix_length = MAX_EMOJI_NAME_LENGTH - 1 - len(str(MAX_FILES_PER_UPLOAD))
    if len(normalized) > max_prefix_length:
        raise ValueError(
            f"Tiền tố quá dài. Tối đa {max_prefix_length} ký tự để chừa chỗ cho số thứ tự."
        )

    return normalized


def generate_emoji_name(prefix: str, sequence_number: int) -> str:
    """
    Generate a predictable Discord emoji name from a prefix and sequence number.
    """
    if sequence_number < 1:
        raise ValueError("Số thứ tự emoji phải bắt đầu từ 1.")

    return f"{prefix}_{sequence_number}"


def get_next_available_emoji_name(
    prefix: str,
    sequence_number: int,
    reserved_names: set[str],
) -> tuple[str, int]:
    """Return the next generated name that is not already reserved."""
    while True:
        emoji_name = generate_emoji_name(prefix, sequence_number)
        sequence_number += 1

        if emoji_name.lower() not in reserved_names:
            return emoji_name, sequence_number


def fingerprint_image(image_bytes: bytes) -> str:
    """Create a content fingerprint from normalized image pixels."""
    digest = hashlib.sha256()

    with Image.open(io.BytesIO(image_bytes)) as image:
        frame_count = getattr(image, "n_frames", 1)

        for frame_index in range(frame_count):
            image.seek(frame_index)
            frame = image.convert("RGBA")
            try:
                frame.thumbnail((128, 128), Image.Resampling.LANCZOS)
                digest.update(frame.size[0].to_bytes(2, "big"))
                digest.update(frame.size[1].to_bytes(2, "big"))
                digest.update(frame.tobytes())
            finally:
                frame.close()

    return digest.hexdigest()


async def collect_existing_emoji_fingerprints(
    emojis: list[discord.Emoji],
    http_client,
) -> tuple[set[str], list[str]]:
    """Download existing emoji assets concurrently and fingerprint them."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CDN_DOWNLOADS)

    async def process_emoji(emoji: discord.Emoji) -> tuple[str | None, str | None]:
        async with semaphore:
            try:
                image_bytes = await http_client.get_from_cdn(emoji.url)
                fingerprint = await asyncio.to_thread(
                    fingerprint_image,
                    image_bytes,
                )
                return fingerprint, None
            except (discord.DiscordException, OSError, ValueError):
                return None, emoji.name

    results = await asyncio.gather(*(process_emoji(emoji) for emoji in emojis))
    fingerprints = {fingerprint for fingerprint, _ in results if fingerprint}
    unavailable = [name for _, name in results if name]

    return fingerprints, unavailable


def is_safe_zip_member(base_dir: Path, member_name: str) -> bool:
    """
    Prevent ZIP path traversal such as ../../something.
    """
    if not member_name or "\x00" in member_name:
        return False

    normalized_name = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(normalized_name)

    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return False

    if ".." in posix_path.parts:
        return False

    base_resolved = base_dir.resolve()
    destination = (base_dir / Path(normalized_name)).resolve()
    return destination == base_resolved or base_resolved in destination.parents


def extract_zip_safely(zip_path: Path, destination: Path) -> list[Path]:
    """
    Validate and extract only supported image members from a ZIP archive.

    The declared and actual uncompressed sizes are bounded to reduce ZIP-bomb
    risk. Unsupported files are never written to disk.
    """
    extracted_files: list[Path] = []

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()

        if len(members) > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP chứa quá nhiều mục.")

        image_members: list[zipfile.ZipInfo] = []
        declared_size = 0
        for member in members:
            if not is_safe_zip_member(destination, member.filename):
                raise ValueError(f"ZIP không an toàn: {member.filename}")

            if member.is_dir():
                continue

            declared_size += member.file_size
            if declared_size > MAX_UNCOMPRESSED_ZIP_SIZE:
                raise ValueError("ZIP có dung lượng sau giải nén vượt giới hạn.")

            if Path(member.filename).suffix.lower() in ALLOWED_EXTENSIONS:
                image_members.append(member)

        if len(image_members) > MAX_FILES_PER_UPLOAD:
            raise ValueError(
                f"ZIP có quá nhiều ảnh. Tối đa {MAX_FILES_PER_UPLOAD} file/lần."
            )

        extracted_size = 0
        for member in image_members:
            output_path = destination / Path(member.filename.replace("\\", "/"))
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with archive.open(member) as src, open(output_path, "wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break

                    extracted_size += len(chunk)
                    if extracted_size > MAX_UNCOMPRESSED_ZIP_SIZE:
                        raise ValueError("ZIP giải nén vượt giới hạn an toàn.")

                    dst.write(chunk)

            extracted_files.append(output_path)

    return extracted_files


def optimize_static_image(path: Path) -> bytes:
    """
    Validate a static image, then convert to PNG and resize if necessary.
    """
    original = path.read_bytes()

    with Image.open(path) as source_image:
        source_image.load()

        if (
            len(original) <= MAX_EMOJI_BYTES
            and path.suffix.lower() == ".png"
            and max(source_image.size) <= 128
        ):
            return original

        image = source_image.convert("RGBA")
        try:
            sizes = [128, 112, 96, 80, 64, 48, 32]

            for size in sizes:
                candidate = image.copy()
                try:
                    candidate.thumbnail((size, size), Image.Resampling.LANCZOS)

                    buffer = io.BytesIO()
                    candidate.save(buffer, format="PNG", optimize=True)
                    data = buffer.getvalue()
                finally:
                    candidate.close()

                if len(data) <= MAX_EMOJI_BYTES:
                    return data
        finally:
            image.close()

    raise ValueError("Không thể nén ảnh xuống giới hạn emoji.")


def prepare_emoji_bytes(path: Path) -> bytes:
    suffix = path.suffix.lower()

    if suffix == ".gif":
        data = path.read_bytes()

        try:
            with Image.open(path) as image:
                frame_count = getattr(image, "n_frames", 1)
                dimensions = image.size
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError("GIF không hợp lệ.") from exc

        needs_resize = max(dimensions) > 128
        if len(data) > MAX_EMOJI_BYTES or needs_resize:
            if frame_count > 1:
                try:
                    return optimize_animated_image(path)
                except ValueError:
                    raise ValueError(
                        "GIF vượt quá giới hạn hoặc kích thước quá lớn "
                        "và không thể tối ưu."
                    )
            return optimize_static_image(path)

        return data

    return optimize_static_image(path)


def optimize_animated_image(path: Path) -> bytes:
    """Convert an animated image to a size-limited GIF without dropping frames."""
    with Image.open(path) as source:
        frame_count = getattr(source, "n_frames", 1)
        if frame_count < 2:
            raise ValueError("Ảnh không có animation.")

        source_frames: list[Image.Image] = []
        durations: list[int] = []
        loop = int(source.info.get("loop", 0))

        try:
            for frame_index in range(frame_count):
                source.seek(frame_index)
                frame = source.convert("RGBA")
                source_frames.append(frame.copy())
                frame.close()
                durations.append(max(20, int(source.info.get("duration", 100))))

            for size in [128, 112, 96, 80, 64, 48, 32]:
                resized_frames: list[Image.Image] = []
                try:
                    for source_frame in source_frames:
                        frame = source_frame.copy()
                        frame.thumbnail((size, size), Image.Resampling.LANCZOS)
                        resized_frames.append(frame)

                    output = io.BytesIO()
                    resized_frames[0].save(
                        output,
                        format="GIF",
                        save_all=True,
                        append_images=resized_frames[1:],
                        duration=durations,
                        loop=loop,
                        optimize=True,
                        disposal=2,
                    )
                    data = output.getvalue()
                finally:
                    for frame in resized_frames:
                        frame.close()

                if len(data) <= MAX_EMOJI_BYTES:
                    return data
        finally:
            for frame in source_frames:
                frame.close()

    raise ValueError("Không thể nén ảnh động xuống giới hạn emoji.")


def prepare_report_emoji_bytes(path: Path) -> bytes:
    """Prepare report artwork while preserving animated WebP frames as GIF."""
    with Image.open(path) as image:
        is_animated = getattr(image, "n_frames", 1) > 1

    if is_animated:
        return optimize_animated_image(path)

    return optimize_static_image(path)


def custom_emoji_token(emoji: discord.Emoji) -> str:
    """Return the correct Discord token for static or animated custom emoji."""
    prefix = "a" if getattr(emoji, "animated", False) else ""
    return f"<{prefix}:{emoji.name}:{emoji.id}>"


def format_report_items(items: list[str]) -> str:
    """Format report details without exceeding a Discord embed field size."""
    if not items:
        return "Không có mục nào."

    lines: list[str] = []
    for item in items[:30]:
        candidate = "\n".join([*lines, f"• {item}"])
        if len(candidate) > 1800:
            break
        lines.append(f"• {item}")

    remaining = len(items) - len(lines)
    if remaining > 0:
        lines.append(f"… và {remaining} mục khác.")

    return "\n".join(lines)


def describe_discord_http_error(error: discord.HTTPException) -> str:
    """Convert Discord's generic HTTP error into a useful upload report message."""
    discord_code = getattr(error, "code", None)
    if discord_code == 30008:
        return "server đã hết slot emoji"
    if discord_code == 50013:
        return "bot thiếu quyền"
    if discord_code == 50035:
        return "dữ liệu ảnh không hợp lệ hoặc Discord từ chối định dạng ảnh"

    status = getattr(error, "status", "?")
    if discord_code is None:
        return f"Discord API lỗi HTTP {status}"
    return f"Discord API lỗi HTTP {status} (code {discord_code})"


async def ensure_report_emojis(guild: discord.Guild) -> dict[str, str]:
    """Find or create the custom emojis used inline in the upload report."""
    report_emojis = dict(REPORT_EMOJI_CACHE.get(guild.id, {}))
    existing_by_name = {emoji.name: emoji for emoji in guild.emojis}

    for key, (emoji_name, asset_filename) in REPORT_EMOJI_ASSETS.items():
        if key in report_emojis:
            continue

        existing = existing_by_name.get(emoji_name)
        if existing is not None:
            report_emojis[key] = custom_emoji_token(existing)
            continue

        asset_path = ASSETS_DIR / asset_filename
        if not asset_path.is_file():
            continue

        try:
            image_bytes = await asyncio.to_thread(
                prepare_report_emoji_bytes,
                asset_path,
            )
            created = await guild.create_custom_emoji(
                name=emoji_name,
                image=image_bytes,
                reason="Create upload report status emoji",
            )
            report_emojis[key] = custom_emoji_token(created)
        except (discord.Forbidden, discord.HTTPException, OSError, ValueError):
            # A full emoji slot or missing permission should not hide the report.
            continue

    REPORT_EMOJI_CACHE[guild.id] = report_emojis
    return {**REPORT_EMOJI_FALLBACKS, **report_emojis}


def build_report_message(
    success: list[str],
    skipped: list[str],
    failed: list[str],
    unavailable_existing_emojis: list[str],
    status_emojis: dict[str, str] | None = None,
) -> discord.Embed:
    """Build the single blue report embed with inline status emojis."""
    status_emojis = {**REPORT_EMOJI_FALLBACKS, **(status_emojis or {})}
    summary = discord.Embed(
        title="Kết quả upload emoji",
        color=0x3498DB,
    )

    sections = [
        f"{status_emojis['success']} **Thành công:** {len(success)}",
        f"{status_emojis['duplicate']} **Trùng/bỏ qua:** {len(skipped)}",
        f"{status_emojis['error']} **Lỗi:** {len(failed)}",
    ]

    if success:
        sections.extend(
            [
                "",
                f"{status_emojis['added']} **Đã thêm:**",
                format_report_items(success),
            ]
        )

    if skipped:
        sections.extend(
            [
                "",
                "⚠️ **Bỏ qua / trùng ảnh:**",
                format_report_items(skipped),
            ]
        )

    if failed:
        sections.extend(
            [
                "",
                "❌ **Lỗi:**",
                format_report_items(failed),
            ]
        )

    if unavailable_existing_emojis:
        sections.extend(
            [
                "",
                "⚠️ **Lưu ý:** "
                "Không kiểm tra được ảnh của "
                f"{len(unavailable_existing_emojis)} emoji cũ.",
            ]
        )

    description = "\n".join(sections)
    if len(description) > 3900:
        description = description[:3850].rstrip() + "\n… Danh sách đã được rút gọn."

    summary.description = description
    summary.set_footer(text="Discord Emoji Upload Bot")
    return summary


class EmojiBot(commands.Bot):
    async def setup_hook(self) -> None:
        if TEST_GUILD_ID.isdigit():
            guild = discord.Object(id=int(TEST_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except (discord.Forbidden, discord.NotFound):
                print(
                    "[SYNC] Cannot access TEST_GUILD_ID. "
                    "Check that the bot is invited to the correct server; "
                    "falling back to global sync."
                )
                synced = await self.tree.sync()
                print(
                    f"[SYNC] {len(synced)} command(s) synced globally. "
                    "Global commands may take time to appear."
                )
            else:
                print(f"[SYNC] {len(synced)} command(s) synced to test guild.")
        else:
            synced = await self.tree.sync()
            print(f"[SYNC] {len(synced)} global command(s) synced.")


intents = discord.Intents.default()
intents.message_content = True
bot = EmojiBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")


def _pick_value(mappings: list[dict], *keys: str, default=None):
    """Return the first non-empty value found in a list of API mappings."""
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", [], {}):
                return value
    return default


def _format_bytes(value) -> str:
    """Format a byte count for a compact Discord embed."""
    try:
        byte_count = float(value)
    except (TypeError, ValueError):
        return "Không rõ"

    if byte_count <= 0:
        return "Không rõ"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def _format_duration(seconds) -> str:
    """Format seconds as m:ss without failing on missing API data."""
    try:
        total_seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "Không rõ"

    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes}:{remaining_seconds:02d}"


def _format_number(value) -> str:
    """Format a count while accepting numeric strings from third-party APIs."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"

    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(int(number)) if number.is_integer() else str(number)


def _format_fps(value) -> str:
    """Format a frame-rate value for the quality section."""
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return "Không rõ"

    return f"{fps:.2f}fps" if not fps.is_integer() else f"{int(fps)}fps"


def _extract_tiktok_tags(info: dict, title: str) -> list[str]:
    """Collect API tags and hashtags from the caption without duplicating them."""
    tags: list[str] = []

    def add_tag(value) -> None:
        if isinstance(value, dict):
            value = value.get("title") or value.get("name") or value.get("desc")
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value:
            return
        if not value.startswith("#"):
            value = f"#{value}"
        if value.lower() not in {tag.lower() for tag in tags}:
            tags.append(value)

    for key in ("hashtags", "tags", "challenges"):
        values = info.get(key, [])
        if isinstance(values, (str, dict)):
            values = [values]
        if isinstance(values, list):
            for value in values:
                add_tag(value)

    for value in re.findall(r"#[\wÀ-ỹ]+", title or ""):
        add_tag(value)

    return tags[:20]


def _quality_label(stream: dict) -> str:
    """Create a compact quality label such as 1440p60 from a video stream."""
    try:
        height = int(float(stream.get("height")))
    except (TypeError, ValueError):
        height = 0

    try:
        width = int(float(stream.get("width")))
    except (TypeError, ValueError):
        width = 0

    if height:
        label = f"{height}p"
    elif width:
        label = f"{width}px"
    else:
        label = str(stream.get("format_note") or "Không rõ")

    try:
        fps = float(stream.get("fps"))
    except (TypeError, ValueError):
        fps = 0

    if fps:
        label += str(int(round(fps)))
    return label


class _QuietYTDLPLogger:
    """Prevent optional metadata probing errors from filling the bot console."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _extract_tiktok_quality(url: str) -> dict:
    """Read stream metadata without downloading the video file."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "logger": _QuietYTDLPLogger(),
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        extracted = downloader.extract_info(url, download=False)

    if not isinstance(extracted, dict):
        return {}

    formats = [
        stream
        for stream in extracted.get("formats", [])
        if isinstance(stream, dict)
        and stream.get("vcodec") not in (None, "none")
    ]

    def stream_score(stream: dict) -> tuple[float, float, float, float]:
        def number(key: str) -> float:
            try:
                return float(stream.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return (
            number("height"),
            number("width"),
            number("fps"),
            number("tbr"),
        )

    best_stream = max(formats, key=stream_score) if formats else extracted
    requested_streams = extracted.get("requested_formats") or []
    selected_stream = next(
        (
            stream
            for stream in requested_streams
            if isinstance(stream, dict)
            and stream.get("vcodec") not in (None, "none")
        ),
        extracted,
    )

    def pick_stream_value(key: str, default=None):
        return (
            best_stream.get(key)
            or selected_stream.get(key)
            or extracted.get(key)
            or default
        )

    quality = {
        "browser_quality": _quality_label(best_stream),
        "phone_quality": _quality_label(selected_stream),
        "width": pick_stream_value("width"),
        "height": pick_stream_value("height"),
        "fps": pick_stream_value("fps"),
        "bitrate": pick_stream_value("tbr"),
        "codec": pick_stream_value("vcodec"),
        "size": pick_stream_value("filesize") or pick_stream_value("filesize_approx"),
    }

    vq_score = (
        extracted.get("VQScore")
        or extracted.get("vq_score")
        or extracted.get("vqScore")
    )
    if vq_score is not None:
        quality["VQScore"] = vq_score

    return {
        key: value
        for key, value in quality.items()
        if value not in (None, "", "Không rõ")
    }


def _probe_video_quality(video_url: str) -> dict:
    """Probe a TikWM video URL directly when TikTok blocks yt-dlp."""
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path or not video_url:
        return {}

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,bit_rate,avg_frame_rate,r_frame_rate:format=size,duration,bit_rate",
        "-of",
        "json",
        video_url,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}

    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}

    streams = metadata.get("streams") or []
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    format_info = metadata.get("format") if isinstance(metadata.get("format"), dict) else {}

    def parse_fps(value):
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            try:
                return float(numerator) / float(denominator)
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    fps = parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    bitrate = stream.get("bit_rate") or format_info.get("bit_rate")
    size = format_info.get("size")
    try:
        if bitrate is not None:
            # Keep ffprobe's bits-per-second value; _build_quality_text
            # normalizes it to Kbps consistently with other sources.
            bitrate = float(bitrate)
    except (TypeError, ValueError):
        bitrate = None

    quality = {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
        "bitrate": bitrate,
        "codec": stream.get("codec_name"),
        "size": size,
    }
    quality["browser_quality"] = _quality_label(quality)
    quality["phone_quality"] = quality["browser_quality"]
    return {key: value for key, value in quality.items() if value not in (None, "")}


def _build_quality_text(info: dict, quality_override: dict | None = None) -> str:
    """Build a truthful quality summary from fields returned by the API."""
    quality_override = quality_override or {}
    nested_video = [
        value
        for key in ("video", "video_info", "video_meta", "metadata")
        if isinstance((value := info.get(key)), dict)
    ]
    mappings = [quality_override, *nested_video, info]

    width = _pick_value(mappings, "width", "video_width")
    height = _pick_value(mappings, "height", "video_height")
    try:
        resolution = f"{int(float(width))}x{int(float(height))}"
    except (TypeError, ValueError):
        resolution = "Không rõ"

    quality_label = _pick_value(
        mappings,
        "definition",
        "ratio",
        "quality",
        "video_quality",
        default=resolution,
    )
    browser_quality = _pick_value(
        mappings,
        "browser_quality",
        "web_quality",
        "definition",
        "ratio",
        default=quality_label,
    )
    phone_quality = _pick_value(
        mappings,
        "phone_quality",
        "mobile_quality",
        "definition",
        "ratio",
        default=quality_label,
    )
    fps = _pick_value(mappings, "fps", "frame_rate", "frameRate")
    codec = _pick_value(
        mappings,
        "codec",
        "codec_type",
        "codecType",
        "vcodec",
        "video_codec",
        default="Không rõ",
    )
    size = _pick_value(
        [quality_override, info, *nested_video],
        "hd_size",
        "size_hd",
        "size",
        "video_size",
    )
    duration = _pick_value([quality_override, info, *nested_video], "duration")
    bitrate = _pick_value(mappings, "bitrate", "bit_rate", "kbps", "tbr")

    if bitrate is None and size and duration:
        try:
            bitrate = float(size) * 8 / float(duration) / 1000
        except (TypeError, ValueError, ZeroDivisionError):
            bitrate = None
    elif bitrate is not None:
        try:
            bitrate = float(bitrate)
            if bitrate > 10_000:
                bitrate /= 1000
        except (TypeError, ValueError):
            bitrate = None

    bitrate_text = f"{bitrate:.0f}" if bitrate is not None else "Không rõ"
    vq_score = _pick_value(
        mappings,
        "VQScore",
        "vq_score",
        "vqScore",
        "quality_score",
        default="Không rõ",
    )

    return (
        f"**Trình duyệt:** {browser_quality}\n"
        f"**Điện thoại:** {phone_quality}\n"
        f"**Gốc:** {resolution} · {_format_fps(fps)}\n"
        f"**Kbps:** {bitrate_text} · {codec} · {_format_bytes(size)}\n"
        f"**Điểm VQ:** {vq_score}"
    )


def _get_access_status(info: dict) -> str:
    """Read visibility when available; otherwise report that the post was accessible."""
    private_value = _pick_value(
        [info],
        "is_private",
        "private",
        "private_account",
    )
    if private_value is True or str(private_value).lower() == "true":
        return "private"

    deleted_value = _pick_value([info], "is_deleted", "deleted")
    if deleted_value is True or str(deleted_value).lower() == "true":
        return "deleted"

    explicit_status = _pick_value([info], "visibility", "privacy")
    return str(explicit_status) if explicit_status else "public"


def _get_shadow_ban_status(info: dict) -> str:
    """Return the requested Yes/No display for the shadow-ban field."""
    shadow_value = _pick_value(
        [info],
        "shadow_ban",
        "shadow_banned",
        "is_shadow_banned",
    )
    if shadow_value is None:
        return "No"
    if shadow_value is True or str(shadow_value).lower() == "true":
        return "Yes"
    return "No"


async def process_tiktok_link(message: discord.Message, url: str):
    def get_emoji(name, default):
        guild_emojis = message.guild.emojis if message.guild else []
        emoji = discord.utils.get(guild_emojis, name=name)
        return str(emoji) if emoji else default

    e_view = get_emoji("suoming_25", "👁️")
    e_like = get_emoji("suoming_14", "❤️")
    e_comment = get_emoji("suoming_7", "💬")
    e_share = get_emoji("suoming_20", "🔄")
    e_music = get_emoji("suoming_41", "🎵")
    e_logo = get_emoji("suoming_56", "📱")

    async with message.channel.typing():
        try:
            import aiohttp
            import datetime
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
            ) as session:
                api_url = f"https://www.tikwm.com/api/?url={url}&hd=1"
                async with session.get(api_url) as resp:
                    data = await resp.json()
            
            if data.get("code") != 0:
                print(f"Lỗi API TikTok: {data.get('msg')}")
                return
                
            info = data.get("data", {})
            author_info = (
                info.get("author")
                if isinstance(info.get("author"), dict)
                else {}
            )
            uploader = author_info.get("nickname", "Unknown")
            uploader_id = author_info.get("unique_id", "unknown")
            title = info.get("title", "Không có caption")
            
            view_count = info.get("play_count", 0)
            like_count = info.get("digg_count", 0)
            comment_count = info.get("comment_count", 0)
            repost_count = info.get("share_count", 0)
            save_count = info.get("collect_count", 0)
            download_count = info.get("download_count", 0)
            
            # Tính tỷ lệ tương tác trên video; API hiện không cung cấp đủ số liệu hồ sơ.
            total_interactions = like_count + comment_count + repost_count + save_count
            engagement_rate = (total_interactions / view_count * 100) if view_count > 0 else 0
            
            # Thời gian đăng & ID
            create_time = info.get("create_time", 0)
            date_str = datetime.datetime.fromtimestamp(create_time).strftime('%d/%m/%Y, %H:%M:%S') if create_time else "Không rõ"
            video_id = info.get("id", "Không rõ")
            region = info.get("region", "Không rõ")
            source_name = _pick_value([info], "source", default="TikWM")
            access_status = _get_access_status(info)
            shadow_ban_status = _get_shadow_ban_status(info)
            tags = _extract_tiktok_tags(info, title)
            category = _pick_value(
                [info],
                "category",
                "category_name",
                "content_category",
                default="Không xác định",
            )
            quality_override: dict = {}
            try:
                quality_override = await asyncio.wait_for(
                    asyncio.to_thread(_extract_tiktok_quality, url),
                    timeout=15,
                )
            except Exception:
                # TikWM data is still useful when yt-dlp cannot inspect the link.
                quality_override = {}
            video_url = info.get("hdplay") or info.get("play")
            if video_url and not {
                "width",
                "height",
                "codec",
            }.issubset(quality_override):
                try:
                    probed_quality = await asyncio.wait_for(
                        asyncio.to_thread(_probe_video_quality, video_url),
                        timeout=20,
                    )
                except Exception:
                    probed_quality = {}
                quality_override = {**quality_override, **probed_quality}
            quality_text = _build_quality_text(info, quality_override)

            embed = discord.Embed(
                title="Phân tích TikTok",
                url=url,
                description=f"**{title}**\n\nNguồn xử lý: `{source_name}`",
                color=0x2F80ED,
            )
            
            embed.set_author(
                name=f"{uploader} · @{uploader_id}",
                url=url,
                icon_url=author_info.get("avatar"),
            )
            
            # Hàng 1
            embed.add_field(name=f"{e_view} Lượt xem", value=_format_number(view_count), inline=True)
            embed.add_field(name=f"{e_like} Lượt thích", value=_format_number(like_count), inline=True)
            embed.add_field(name=f"{e_comment} Bình luận", value=_format_number(comment_count), inline=True)
            
            # Hàng 2
            embed.add_field(name=f"{e_share} Chia sẻ", value=_format_number(repost_count), inline=True)
            embed.add_field(name="⭐ Yêu thích", value=_format_number(save_count), inline=True)
            embed.add_field(name="📥 Tải xuống", value=_format_number(download_count), inline=True)
            
            # Âm thanh
            music_info = (
                info.get("music_info")
                if isinstance(info.get("music_info"), dict)
                else {}
            )
            music_title = music_info.get("title", info.get("music", "Âm thanh gốc"))
            music_author = music_info.get("author")
            music_duration = music_info.get("duration") or info.get("duration")
            music_text = str(music_title)
            if music_author:
                music_text += f" · {music_author}"
            if music_duration:
                music_text += f" · {_format_duration(music_duration)}"
            embed.add_field(name=f"{e_music} Âm thanh", value=music_text, inline=False)
            
            # Thông tin chi tiết
            info_text = (
                f"**Đã đăng:** {date_str}\n"
                f"**ID:** {video_id}\n"
                f"**Nguồn:** {source_name}\n"
                f"**Khu vực:** {region}\n"
                f"**Trạng thái truy cập:** {access_status}\n"
                f"**Shadow ban:** {shadow_ban_status}"
            )
            embed.add_field(name="📊 Thông tin", value=info_text, inline=False)
            
            # Sức khỏe hồ sơ
            health_text = (
                f"**Tương tác:** {engagement_rate:.2f}%\n"
                f"**Lượt tương tác:** {_format_number(total_interactions)} / "
                f"{_format_number(view_count)}"
            )
            embed.add_field(name="📈 Sức khỏe hồ sơ", value=health_text, inline=False)

            # Chất lượng
            embed.add_field(name="🎞️ Chất lượng", value=quality_text, inline=False)

            # Danh mục và hashtag
            tag_text = " ".join(tags) if tags else "Không có hashtag"
            category_text = (
                f"**Danh mục:** {category}\n"
                if category != "Không xác định"
                else ""
            )
            embed.add_field(
                name="🏷️ Danh mục / Tag",
                value=f"{category_text}{tag_text}",
                inline=False,
            )
            
            # Link tải
            if "play" in info:
                embed.add_field(name="⬇️ Link gốc", value=f"[Tải video không logo]({info['play']})", inline=False)
            
            thumbnail = info.get("cover")
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            
            embed.set_footer(text="TikTok Analytics")
            
            try:
                await message.reply(embed=embed, mention_author=False)
            except discord.NotFound:
                # The source message may have been deleted before the response.
                await message.channel.send(embed=embed)
            except discord.HTTPException as exc:
                if getattr(exc, "code", None) != 50035:
                    raise
                # Discord rejects the message reference when the source message
                # no longer exists, so retry as a normal channel message.
                await message.channel.send(embed=embed)
            
            try:
                await message.edit(suppress=True)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
                
        except Exception as e:
            print(f"Lỗi khi cào TikTok: {e}")




@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    tiktok_match = re.search(r'https?://(?:www\.|vt\.|vm\.)?tiktok\.com/[^\s]+', message.content)
    if tiktok_match:
        url = tiktok_match.group(0).rstrip(".,!?)]}>")
        await process_tiktok_link(message, url)

    await bot.process_commands(message)



@bot.tree.command(name="ping", description="Kiểm tra bot có đang hoạt động không.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! {round(bot.latency * 1000)} ms",
        ephemeral=True,
    )


@bot.tree.command(
    name="upload-emojis",
    description="Upload hàng loạt emoji từ một file ZIP.",
)
@app_commands.describe(
    file="File ZIP chứa PNG/JPG/JPEG/GIF/WEBP",
    prefix="Tiền tố tên emoji, ví dụ pepe sẽ tạo pepe_1, pepe_2",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_emojis_and_stickers=True)
async def upload_emojis(
    interaction: discord.Interaction,
    file: discord.Attachment,
    prefix: str = "emoji",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng trong Discord Server.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None or not (
        bot_member.guild_permissions.manage_emojis_and_stickers
        or bot_member.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "Bot cần quyền quản lý emoji/expressions để thực hiện upload.",
            ephemeral=True,
        )
        return

    if not file.filename.lower().endswith(".zip"):
        await interaction.response.send_message(
            "Bạn phải gửi file `.zip`.",
            ephemeral=True,
        )
        return

    if file.size > MAX_ZIP_SIZE:
        await interaction.response.send_message(
            f"ZIP quá lớn. Giới hạn hiện tại: {MAX_ZIP_SIZE // (1024 * 1024)} MB.",
            ephemeral=True,
        )
        return

    try:
        emoji_prefix = normalize_emoji_prefix(prefix)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    success: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    temp_dir = Path(tempfile.mkdtemp(prefix="emoji_upload_"))

    try:
        zip_path = temp_dir / "upload.zip"
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            await file.save(zip_path)
        except (discord.HTTPException, OSError):
            await interaction.followup.send(
                "Không thể tải file ZIP xuống. Vui lòng thử lại.",
                ephemeral=True,
            )
            return

        if zip_path.stat().st_size > MAX_ZIP_SIZE:
            await interaction.followup.send(
                f"ZIP quá lớn. Giới hạn hiện tại: {MAX_ZIP_SIZE // (1024 * 1024)} MB.",
                ephemeral=True,
            )
            return

        try:
            extracted = await asyncio.to_thread(
                extract_zip_safely,
                zip_path,
                extract_dir,
            )
        except zipfile.BadZipFile:
            await interaction.followup.send(
                "File ZIP bị lỗi hoặc không đúng định dạng.",
                ephemeral=True,
            )
            return
        except (OSError, ValueError) as exc:
            await interaction.followup.send(
                f"Không thể xử lý ZIP: {exc}",
                ephemeral=True,
            )
            return

        image_files = [
            path
            for path in extracted
            if path.suffix.lower() in ALLOWED_EXTENSIONS
        ]

        if not image_files:
            await interaction.followup.send(
                "Không tìm thấy PNG/JPG/JPEG/GIF/WEBP trong ZIP.",
                ephemeral=True,
            )
            return

        existing_names = {emoji.name.lower() for emoji in interaction.guild.emojis}
        batch_names: set[str] = set()
        existing_image_fingerprints, unavailable_existing_emojis = (
            await collect_existing_emoji_fingerprints(
                interaction.guild.emojis,
                interaction.client.http,
            )
        )
        batch_image_fingerprints: set[str] = set()

        next_sequence_number = 1

        for path in image_files:
            file_sequence_number = next_sequence_number
            next_sequence_number += 1
            display_name = generate_emoji_name(emoji_prefix, file_sequence_number)

            try:
                emoji_bytes = await asyncio.to_thread(prepare_emoji_bytes, path)
                image_fingerprint = await asyncio.to_thread(
                    fingerprint_image,
                    emoji_bytes,
                )

                if (
                    image_fingerprint in existing_image_fingerprints
                    or image_fingerprint in batch_image_fingerprints
                ):
                    skipped.append(f"{display_name}: ảnh trùng")
                    continue

                emoji_name, available_sequence_number = get_next_available_emoji_name(
                    emoji_prefix,
                    file_sequence_number,
                    existing_names | batch_names,
                )
                next_sequence_number = max(
                    next_sequence_number,
                    available_sequence_number,
                )
                display_name = emoji_name

                # Reserve the generated name before processing the upload so
                # every different image receives a unique name.
                batch_names.add(emoji_name.lower())

                await interaction.guild.create_custom_emoji(
                    name=emoji_name,
                    image=emoji_bytes,
                    reason=f"Bulk emoji upload requested by {interaction.user}",
                )

                success.append(emoji_name)
                batch_image_fingerprints.add(image_fingerprint)

                # Keep a small gap between uploads; discord.py also handles
                # Discord's route rate limits for the API request itself.
                await asyncio.sleep(UPLOAD_DELAY_SECONDS)

            except discord.Forbidden:
                failed.append(f"{display_name}: bot thiếu quyền")
            except discord.HTTPException as exc:
                failed.append(f"{display_name}: {describe_discord_http_error(exc)}")
            except Exception as exc:
                failed.append(f"{display_name}: {exc}")

        report_emojis = await ensure_report_emojis(interaction.guild)
        report_embed = build_report_message(
            success,
            skipped,
            failed,
            unavailable_existing_emojis,
            report_emojis,
        )
        await interaction.followup.send(
            embed=report_embed,
            ephemeral=True,
        )

    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)


@upload_emojis.error
async def upload_emojis_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.NoPrivateMessage):
        message = "Lệnh này chỉ dùng trong Discord Server."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "Bạn cần quyền quản lý emoji/expressions để dùng lệnh này."
    else:
        message = f"Có lỗi xảy ra: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Không tìm thấy DISCORD_TOKEN. "
            "Hãy tạo file .env dựa trên .env.example."
        )

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
