from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_ZIP_SIZE = 25 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_UNCOMPRESSED_ZIP_SIZE = 100 * 1024 * 1024
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

        if len(original) <= MAX_EMOJI_BYTES and path.suffix.lower() == ".png":
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
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError("GIF không hợp lệ.") from exc

        if len(data) > MAX_EMOJI_BYTES:
            raise ValueError(
                "GIF vượt quá giới hạn. Phiên bản hiện tại chưa tối ưu GIF động."
            )

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
# This bot exposes slash commands only; no message-content intent is needed.
bot = EmojiBot(command_prefix=None, intents=intents)


@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")


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
                failed.append(f"{display_name}: Discord API lỗi ({exc.status})")
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
