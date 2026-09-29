<p align="center">
  <img src="assets/readme-banner.svg" alt="Qingxiao EJ - Discord Emoji Upload Bot" width="100%">
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord&logoColor=white" alt="discord.py 2.x"></a>
  <a href="https://python-pillow.org/"><img src="https://img.shields.io/badge/Pillow-image%20processing-45B8AC" alt="Pillow"></a>
  <img src="https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white" alt="Windows">
</p>

<h1 align="center">Qingxiao EJ · Discord Emoji Upload Bot</h1>

<p align="center">
  Upload hàng loạt emoji vào Discord Server từ một file ZIP — an toàn, rõ ràng và dễ sử dụng.
</p>

<p align="center">
  <img src="assets/status_success.webp" width="72" alt="Success status emoji">
  <img src="assets/status_duplicate.webp" width="72" alt="Duplicate status emoji">
  <img src="assets/status_error.webp" width="72" alt="Error status emoji">
  <img src="assets/status_added.webp" width="72" alt="Added status emoji">
</p>

> Đây là project bot Discord v1 tập trung vào một luồng đơn giản: nhận ZIP, kiểm tra an toàn, xử lý ảnh, upload lần lượt và trả report dễ đọc.

## Mục lục

- [Tổng quan](#tổng-quan)
- [Bot giải quyết vấn đề gì?](#bot-giải-quyết-vấn-đề-gì)
- [Tính năng nổi bật](#tính-năng-nổi-bật)
- [Yêu cầu](#yêu-cầu)
- [Cài đặt nhanh trên Windows](#cài-đặt-nhanh-trên-windows)
- [Tạo Discord Application](#tạo-discord-application)
- [Cấu hình quyền bot](#cấu-hình-quyền-bot)
- [Chạy bot](#chạy-bot)
- [Sử dụng](#sử-dụng)
- [Luồng xử lý](#luồng-xử-lý)
- [Quy tắc đặt tên](#quy-tắc-đặt-tên)
- [Xử lý ảnh và GIF](#xử-lý-ảnh-và-gif)
- [Giới hạn hiện tại](#giới-hạn-hiện-tại)
- [Bảo mật](#bảo-mật)
- [Cấu trúc project](#cấu-trúc-project)
- [FAQ và xử lý sự cố](#faq-và-xử-lý-sự-cố)
- [Roadmap](#roadmap)
- [Tech stack](#tech-stack)

## Tổng quan

**Qingxiao EJ** là Discord bot dùng slash command để đưa nhiều emoji lên server từ một file ZIP.

Thay vì upload từng ảnh thủ công, người dùng chỉ cần:

1. Chuẩn bị một file `emoji.zip`.
2. Chạy `/upload-emojis`.
3. Chọn ZIP và nhập tiền tố tên emoji.
4. Chờ bot xử lý và xem report thành công, trùng hoặc lỗi.

Bot không dùng selfbot, user token, browser automation hoặc API không chính thức.

## Bot giải quyết vấn đề gì?

Upload nhiều emoji thủ công thường tốn thời gian và dễ gặp các vấn đề:

- Tên file chứa ký tự Discord không chấp nhận.
- Ảnh vượt giới hạn dung lượng.
- ZIP chứa file lạ hoặc đường dẫn nguy hiểm.
- Upload lại ảnh cũ nhưng không biết ảnh đã tồn tại.
- Một ảnh lỗi làm gián đoạn cả batch.

Bot xử lý từng ảnh độc lập, tiếp tục khi một ảnh lỗi và trả kết quả cuối cùng theo từng nhóm.

## Tính năng nổi bật

### Discord

- Slash command `/ping`.
- Slash command `/upload-emojis`.
- Chỉ cho phép upload trong Discord Server.
- Kiểm tra quyền của user và bot.
- Đồng bộ command theo `TEST_GUILD_ID` để test nhanh hoặc sync global.

### ZIP an toàn

- Chỉ nhận file `.zip`.
- Chặn ZIP path traversal như `../../secret.txt`, đường dẫn tuyệt đối và path Windows nguy hiểm.
- Giới hạn số entry, số ảnh và dung lượng sau giải nén.
- Không chạy bất kỳ file hoặc script nào trong ZIP.
- Tự dọn thư mục tạm sau khi xử lý.

### Ảnh và emoji

- Hỗ trợ `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`.
- Ảnh tĩnh được kiểm tra, resize giữ tỷ lệ và nén khi cần.
- GIF hợp lệ được giữ animation nếu nằm trong giới hạn.
- Phát hiện ảnh trùng bằng fingerprint nội dung, không chỉ dựa trên tên file.
- Tự chọn số tiếp theo nếu tên emoji đã tồn tại.
- Report dùng Embed màu xanh biển cùng custom emoji trạng thái động.

## Demo command

### Kiểm tra bot

```text
/ping
```

### Upload một batch

```text
/upload-emojis file:emoji.zip prefix:pepe
```

Kết quả tên emoji sẽ có dạng:

```text
pepe_1
pepe_2
pepe_3
```

`prefix` là tùy chọn. Nếu bỏ trống, bot dùng tiền tố mặc định `emoji`.

## Yêu cầu

- Windows 10/11.
- Python 3.11 trở lên.
- VS Code là tùy chọn nhưng được khuyến nghị.
- Extension VS Code:
  - Python — Microsoft.
  - Pylance — Microsoft.
- Một Discord Application có Bot User.
- Bot đã được mời vào server cần upload emoji.

## Cài đặt nhanh trên Windows

Mở PowerShell hoặc terminal trong VS Code tại thư mục project:

### 1. Tạo virtual environment

```powershell
py -3.11 -m venv .venv
```

### 2. Kích hoạt môi trường

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Command Prompt:

```bat
.venv\Scripts\activate
```

### 3. Cài dependency

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Tạo file `.env`

PowerShell:

```powershell
Copy-Item .env.example .env
```

Sau đó mở `.env` và điền:

```env
DISCORD_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE
TEST_GUILD_ID=
```

Không đưa token vào `bot.py`, README, Git hoặc tin nhắn công khai.

## Tạo Discord Application

1. Mở [Discord Developer Portal](https://discord.com/developers/applications).
2. Chọn **New Application**.
3. Đặt tên application, ví dụ `Qingxiao EJ`.
4. Mở mục **Bot** và chọn **Add Bot**.
5. Tạo hoặc reset token.
6. Copy token vào `.env` ở biến `DISCORD_TOKEN`.

### Mời bot vào server

Trong **OAuth2 → URL Generator**:

- Scopes:
  - `bot`
  - `applications.commands`
- Bot Permissions:
  - `View Channels`
  - `Send Messages`
  - `Use Application Commands`
  - `Manage Expressions` hoặc `Create Expressions`

Message Content Intent không cần bật vì bot chỉ dùng slash command.

## Cấu hình quyền bot

### User

User chạy `/upload-emojis` cần quyền quản lý emoji/expressions tương ứng.

### Bot

Bot cần quyền:

- Xem channel.
- Gửi message.
- Dùng application commands.
- Tạo hoặc quản lý expressions.

Để hiển thị 4 emoji trạng thái động inline, server cần khoảng 4 slot emoji trống. Nếu không đủ slot, bot tự dùng emoji Unicode dự phòng và vẫn gửi report.

## Chạy bot

Sau khi activate `.venv`:

```powershell
python bot.py
```

Hoặc:

```powershell
py bot.py
```

Trong VS Code, nhấn `F5` và chọn profile:

```text
Run Discord Emoji Bot
```

Log thành công thường có dạng:

```text
[SYNC] 2 command(s) synced to test guild.
[READY] Logged in as ...
```

Nếu chỉ sync global, command có thể xuất hiện chậm hơn so với test guild.

## Sử dụng

### `/ping`

Kiểm tra bot có online và phản hồi được hay không.

### `/upload-emojis`

Các trường nhập:

| Trường | Bắt buộc | Mô tả |
|---|:---:|---|
| `file` | Có | File ZIP chứa ảnh emoji |
| `prefix` | Không | Tiền tố tên, mặc định là `emoji` |

Ví dụ:

```text
prefix = Pepe Smile!!
```

Bot chuẩn hóa thành:

```text
pepe_smile_1
pepe_smile_2
```

## Luồng xử lý

```text
Người dùng chọn emoji.zip
        ↓
Kiểm tra extension và dung lượng
        ↓
Giải nén an toàn vào thư mục tạm
        ↓
Tìm PNG / JPG / JPEG / GIF / WEBP
        ↓
Chuẩn hóa prefix và tạo số thứ tự
        ↓
Kiểm tra ảnh hỏng, ảnh trùng và tên đã tồn tại
        ↓
Resize / compress ảnh tĩnh nếu cần
        ↓
Upload lần lượt bằng discord.py
        ↓
Tạo Embed report và dọn thư mục tạm
```

## Cấu trúc ZIP mẫu

```text
emoji.zip
├── happy.png
├── angry.jpg
├── cat_laugh.gif
├── pepe_smile.webp
└── subfolder/
    └── hello.jpeg
```

File không phải PNG/JPG/JPEG/GIF/WEBP sẽ được bỏ qua và không được thực thi.

## Quy tắc đặt tên

Tên file gốc chỉ được dùng để nhận diện loại ảnh; tên emoji được tạo từ `prefix`.

```text
prefix nhập vào:  Pepe Smile!!
prefix sau chuẩn hóa: pepe_smile

ảnh thứ 1 → pepe_smile_1
ảnh thứ 2 → pepe_smile_2
ảnh thứ 3 → pepe_smile_3
```

Quy tắc:

- Chuyển thành chữ thường.
- Ký tự không hợp lệ được đổi thành `_`.
- Gộp nhiều dấu `_` liên tiếp.
- Chỉ dùng chữ cái, số và underscore.
- Tiền tố có giới hạn để chừa chỗ cho số thứ tự.
- Nếu tên đã tồn tại trên server, bot tự tìm số tiếp theo còn trống.
- Nếu ảnh trùng, bot bỏ qua ảnh đó nhưng không làm hỏng các ảnh còn lại.

## Xử lý ảnh và GIF

| Loại file | Cách xử lý |
|---|---|
| PNG | Kiểm tra và nén/resize nếu vượt giới hạn |
| JPG/JPEG | Chuyển sang PNG, giữ tỷ lệ và nén nếu cần |
| WEBP | Kiểm tra, chuyển sang định dạng phù hợp cho emoji |
| GIF hợp lệ | Giữ animation nếu không vượt giới hạn |
| GIF quá lớn | Báo lỗi; v1 chưa tự tối ưu GIF upload |
| File giả hoặc ảnh hỏng | Ghi vào danh sách lỗi và tiếp tục batch |

Các ảnh trạng thái trong `assets/` là WebP động. Bot chuyển chúng sang GIF trước khi tạo custom emoji để animation được giữ lại.

## Duplicate detection

Bot kiểm tra hai lớp:

1. So sánh fingerprint với các emoji đã có trên server.
2. So sánh fingerprint giữa các ảnh trong cùng ZIP.

Vì vậy, hai file có tên khác nhau nhưng cùng nội dung vẫn được xem là trùng.

Ví dụ:

```text
cat.png
cat-copy.jpg
```

Nếu nội dung giống nhau, một file sẽ được upload và file còn lại được ghi vào mục `Trùng/bỏ qua`.

## Report kết quả

Bot gửi một Embed màu xanh biển gồm:

- Dòng tổng kết `Thành công`.
- Dòng tổng kết `Trùng/bỏ qua`.
- Dòng tổng kết `Lỗi`.
- Chi tiết những emoji đã thêm.
- Chi tiết ảnh trùng hoặc bị bỏ qua.
- Chi tiết lỗi từng file.

Bot tạo hoặc tái sử dụng 4 custom emoji trạng thái động:

```text
report_success_gif
report_duplicate_gif
report_error_gif
report_added_gif
```

Nếu server thiếu slot, thiếu quyền hoặc chưa có file asset, report vẫn hoạt động với emoji Unicode dự phòng.

## Giới hạn hiện tại

| Hạng mục | Giới hạn |
|---|---:|
| Dung lượng ZIP | 25 MB |
| Số entry trong ZIP | 500 |
| Dung lượng sau giải nén | 100 MB |
| Số ảnh mỗi lần upload | 100 |
| Dung lượng emoji đầu ra | 256 KB |
| Kích thước resize ảnh tĩnh | 128, 112, 96, 80, 64, 48 hoặc 32 px |

Ngoài giới hạn của bot, Discord Server vẫn có giới hạn riêng về số slot emoji và quyền expressions.

## Bảo mật

- Token chỉ đọc từ `.env`.
- `.env` nằm trong `.gitignore`.
- Không log hoặc hiển thị token.
- Chặn ZIP path traversal.
- Giới hạn ZIP bomb bằng số entry và dung lượng sau giải nén.
- Không chạy `.exe`, `.py`, `.js`, `.bat`, `.cmd`, `.ps1`, `.sh` hoặc file lạ trong ZIP.
- File tạm được xóa trong `finally` kể cả khi có lỗi hoặc return sớm.
- Chỉ dùng official Discord API thông qua `discord.py`.
- Không tích hợp Ollama trong v1.

## Cấu trúc project

```text
discord-emoji-bot/
├── .vscode/
│   ├── settings.json
│   └── launch.json
├── assets/
│   ├── readme-banner.svg
│   ├── status_success.webp
│   ├── status_duplicate.webp
│   ├── status_error.webp
│   └── status_added.webp
├── temp/                    # File tạm trong lúc xử lý
├── bot.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── PLAN.md
```

## FAQ và xử lý sự cố

### Command không xuất hiện trong Discord

- Kiểm tra bot đã được mời với scope `applications.commands`.
- Kiểm tra `TEST_GUILD_ID` là ID đúng của server.
- Kiểm tra bot có mặt trong server đó.
- Restart bot để chạy lại sync.
- Nếu đang sync global, Discord có thể cần thêm thời gian để cập nhật.

### Lỗi `403 Missing Access` khi sync

Bot đang sync vào server không truy cập được. Kiểm tra `TEST_GUILD_ID`, invite đúng server và quyền bot.

### Bot báo tất cả ảnh là trùng

Đây thường là hành vi đúng khi bạn upload lại ảnh đã có trên server hoặc ảnh trùng nhau trong ZIP. Bot so sánh nội dung ảnh, không chỉ tên file.

### Emoji trạng thái không chuyển động

Bot mới dùng tên custom emoji có hậu tố `_gif`. Cần restart bot và có ít nhất 4 slot emoji trống. Nếu server còn các emoji tĩnh cũ từ bản trước, có thể xóa các emoji `report_success`, `report_duplicate`, `report_error` và `report_added` trong Server Settings → Expressions.

### GIF upload bị báo quá lớn

GIF upload được giữ animation nhưng hiện chưa được tự tối ưu. Hãy giảm dung lượng GIF trước khi đưa vào ZIP.

### Bot hiển thị “đang suy nghĩ” lâu

Bot cần tải và fingerprint các emoji cũ để kiểm tra ảnh trùng, sau đó mới upload batch. Server càng nhiều emoji hoặc ZIP càng lớn thì thời gian xử lý càng tăng.

## Roadmap

Các mục sau chưa triển khai trong v1:

- Progress bar hoặc cập nhật tiến trình trực tiếp.
- Hủy batch đang chạy.
- Dry-run / preview trước khi upload.
- Nhiều ZIP trong một lần chạy.
- Sticker support.
- Tối ưu GIF upload lớn.
- Lưu lịch sử upload.
- Docker và logging file.
- Ollama AI naming hoặc phân loại emoji.

## Tech stack

- **Python 3.11+** — runtime.
- **discord.py** — official Discord API wrapper.
- **Pillow** — đọc, validate, resize, compress và fingerprint ảnh.
- **python-dotenv** — đọc biến môi trường từ `.env`.
- **zipfile / pathlib / tempfile / asyncio** — xử lý ZIP, file tạm và tác vụ async.

## Kiểm tra nhanh trước khi dùng

```powershell
python -m py_compile bot.py
python bot.py
```

Sau đó kiểm tra lần lượt:

```text
/ping
/upload-emojis
```

---

<p align="center">
  Built with Python, Pillow and discord.py · Qingxiao EJ
</p>

<p align="center"><a href="#qingxiao-ej--discord-emoji-upload-bot">Quay lại đầu trang</a></p>
