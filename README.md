# Hermes ↔ Facebook Messenger Gateway

Plugin platform adapter kết nối **Hermes Agent** với **Facebook Messenger**.
Người dùng nhắn tin cho Facebook Page của bạn → Hermes agent trả lời.

Plugin cài vào **thư mục plugin của người dùng** (`~/.hermes/plugins/messenger/`),
**nằm ngoài** repo `hermes-agent`. Nhờ vậy bạn vẫn chạy được `hermes update`
để lấy code mới mà **không bao giờ mất / xung đột** với gateway Messenger.

> 1:1 DM only. Webhook + xác thực chữ ký `X-Hub-Signature-256` (HMAC-SHA256
> App Secret). Gửi qua Graph API Send API trong cửa sổ 24h.

---

## 0. Vì sao an toàn với `hermes update`

`hermes update` (bản git) chạy `git fetch && git reset --hard origin/<branch>` —
xoá sạch mọi thay đổi trong cây repo. Hermes quét plugin ở **3 nơi**, trong đó
`~/.hermes/plugins/<name>/` nằm **ngoài** repo và **không bị** `git reset` đụng
tới. `$HERMES_HOME` (`~/.hermes/`) cũng không bị git pull chạm vào.

→ Đặt plugin tại `~/.hermes/plugins/messenger/` + bật trong
`~/.hermes/config.yaml`. Update bao nhiêu lần cũng còn nguyên.

| Vị trí | Update có xoá? |
|--------|----------------|
| `hermes-agent/plugins/platforms/` (bundled) | ✗ Bị `reset --hard` xoá |
| **`~/.hermes/plugins/messenger/` (user)** | ✓ An toàn |
| Sửa thẳng core (`config.py`, `run.py`…) | ✗ Xung đột mỗi lần update |

---

## 1. Yêu cầu trước (làm trên Meta — bắt buộc thủ công)

Các bước này phải làm trong Meta Developer Dashboard; lấy 3 giá trị bí mật rồi
đưa vào Hermes.

1. **Tạo Meta App** (loại *Business*): https://developers.facebook.com/apps/ →
   ghi lại **App Secret** (Settings → Basic).
2. **Add product Messenger** vào app.
3. **Facebook Page**: Messenger → Settings → *Access Tokens* → kết nối Page →
   **Generate Page Access Token** (long-lived). Đây là `MESSENGER_PAGE_ACCESS_TOKEN`.
4. **Verify Token**: tự nghĩ một chuỗi ngẫu nhiên bất kỳ (vd UUID). Đây là
   `MESSENGER_VERIFY_TOKEN` — dán **cùng giá trị** vào cả Meta và Hermes.
5. **Webhook Callback URL** (HTTPS công khai): Messenger → Settings → Webhooks →
   Callback URL = `https://<public-host>/messenger/webhook`, Verify Token = ở
   bước 4, **subscribe fields**: `messages`, `messaging_postbacks`.
6. **Subscribe Page** vào app (panel Webhooks).
7. **Quyền**: `pages_messaging`. Lúc Dev Mode chỉ admin/tester/role của app mới
   nhắn được bot; muốn mở cho public cần App Review + Business Verification.
8. **HTTPS cho máy local**: dùng tunnel —
   `cloudflared tunnel --url http://localhost:8650` hoặc `ngrok http 8650`.

> PSID (page-scoped user ID) chỉ có sau khi user nhắn Page lần đầu — không tra
> trước được. Lấy PSID từ log lần đầu để bỏ vào allowlist.

---

## 2. Cài đặt (cho người đã có sẵn Hermes Agent)

### Cách A — script tự động (khuyến nghị)

```bash
git clone https://github.com/Shaney17/hermess-messenger.git
cd hermess-messenger
./install.sh
```

Script sẽ: copy `messenger/` → `~/.hermes/plugins/messenger/`, và thêm
`messenger` vào `plugins.enabled` trong `~/.hermes/config.yaml` (idempotent).

> `HERMES_HOME` khác mặc định? Chạy: `HERMES_HOME=/duong/dan ./install.sh`

### Cách B — thủ công

```bash
git clone https://github.com/Shaney17/hermess-messenger.git
mkdir -p ~/.hermes/plugins
cp -R hermess-messenger/messenger ~/.hermes/plugins/messenger
```

Thêm vào `~/.hermes/config.yaml` (user plugin bị gate bởi `plugins.enabled`,
khác với bundled plugin tự load):

```yaml
plugins:
  enabled:
    - messenger
```

---

## 3. Cấu hình secrets

Thêm vào `~/.hermes/.env` (file này nằm trong `$HERMES_HOME` → cũng sống sót qua update):

```dotenv
MESSENGER_PAGE_ACCESS_TOKEN=EAAB...          # bắt buộc
MESSENGER_APP_SECRET=xxxxxxxx                 # bắt buộc
MESSENGER_VERIFY_TOKEN=chuoi-ngau-nhien-cua-ban  # bắt buộc

# Tuỳ chọn:
MESSENGER_PORT=8650
MESSENGER_PUBLIC_URL=https://my-tunnel.example.com
MESSENGER_ALLOWED_USERS=PSID1,PSID2          # allowlist
MESSENGER_ALLOW_ALL_USERS=true               # CHỈ dùng khi dev
MESSENGER_HOME_CHANNEL=PSID                   # đích mặc định cho cron
MESSENGER_API_VERSION=v21.0
```

Hoặc dùng wizard: `hermes setup messenger`.

---

## 4. Chạy & kiểm tra

```bash
# 1. Mở tunnel HTTPS
cloudflared tunnel --url http://localhost:8650

# 2. Trong Meta console: Callback URL = https://<tunnel>/messenger/webhook
#    + Verify Token giống .env → Meta gọi GET handshake (plugin tự trả lời)
#    + subscribe fields messages, messaging_postbacks + subscribe Page

# 3. Khởi động gateway
hermes gateway

# 4. Xác nhận plugin đã load (thấy Messenger 💬)
hermes gateway status

# 5. Nhắn tin cho Page bằng tài khoản test → xem log → agent trả lời
```

Health check: `GET https://<tunnel>/messenger/webhook/health` → `{"status":"ok"}`.

---

## 5. Kiểm thử update an toàn

```bash
hermes update
ls ~/.hermes/plugins/messenger      # vẫn còn nguyên
hermes gateway status               # vẫn thấy Messenger
```

---

## 6. Biến môi trường

| Biến | Bắt buộc | Ý nghĩa |
|------|----------|---------|
| `MESSENGER_PAGE_ACCESS_TOKEN` | ✅ | Page access token (auth Send API) |
| `MESSENGER_APP_SECRET` | ✅ | Xác thực chữ ký webhook HMAC-SHA256 |
| `MESSENGER_VERIFY_TOKEN` | ✅ | Chuỗi tự chọn cho GET handshake |
| `MESSENGER_PORT` | ➖ | Cổng webhook (mặc định 8650) |
| `MESSENGER_HOST` | ➖ | Host bind (mặc định 0.0.0.0) |
| `MESSENGER_PUBLIC_URL` | ➖ | Base URL HTTPS công khai |
| `MESSENGER_API_VERSION` | ➖ | Graph API version (mặc định `v21.0`) |
| `MESSENGER_ALLOWED_USERS` | ➖ | PSID được phép, phân tách bằng dấu phẩy |
| `MESSENGER_ALLOW_ALL_USERS` | ➖ | Cho phép mọi user (dev) |
| `MESSENGER_HOME_CHANNEL` | ➖ | PSID đích cho cron/notification |
| `MESSENGER_CRON_TAG` | ➖ | Message tag cho cron ngoài cửa sổ 24h |

---

## 7. Giới hạn & lưu ý

- **Cửa sổ 24h**: `messaging_type=RESPONSE` chỉ hợp lệ trong 24h kể từ tin nhắn
  cuối của user. Gửi chủ động (cron) ngoài 24h cần message tag đã duyệt — đặt
  `MESSENGER_CRON_TAG` (vd `CONFIRMED_EVENT_UPDATE`).
- **2000 ký tự/tin**: reply dài tự cắt thành nhiều bubble.
- **Không render Markdown**: `**`, `#` hiện literal; URL trần thì auto-link.
- **1:1 only**: Messenger Platform không có group.
- **Graph API version**: kiểm tra version còn hỗ trợ trước khi deploy; override
  bằng `MESSENGER_API_VERSION`.
- **Tách lịch sử theo user**: PSID đã là Page-scoped theo thiết kế của Meta —
  cùng 1 người nhắn 2 Page khác nhau cho ra 2 PSID khác nhau. Hermes tạo session
  key dạng `messenger:dm:<PSID>`, mỗi user có lịch sử riêng biệt.

---

## 8. Chạy test (dev)

```bash
# Cần repo hermes-agent trên PYTHONPATH để resolve gateway.*
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q

# Hoặc dùng uv với deps của hermes-agent:
cd /path/to/hermes-agent
PYTHONPATH=/path/to/hermess-messenger \
  uv run --with pytest --with aiohttp python -m pytest /path/to/hermess-messenger/tests -q
```

---

## 9. Troubleshooting

| Triệu chứng | Nguyên nhân / cách xử lý |
|-------------|--------------------------|
| `hermes gateway status` không thấy Messenger | Thiếu `messenger` trong `plugins.enabled`; hoặc thiếu 3 biến bắt buộc. Bật `HERMES_PLUGINS_DEBUG=1` xem log. |
| Meta báo webhook verify thất bại | `MESSENGER_VERIFY_TOKEN` không khớp; hoặc tunnel chưa chạy/không HTTPS. |
| Webhook POST trả 401 | `MESSENGER_APP_SECRET` sai (chữ ký không khớp). |
| Bot không trả lời | PSID không nằm trong allowlist (đặt `MESSENGER_ALLOW_ALL_USERS=true` khi test); hoặc Page chưa subscribe app. |
| Lỗi gửi ngoài 24h | Cần message tag — đặt `MESSENGER_CRON_TAG`. |

---

## Architecture

```
Messenger user
   │  DM
   ▼
Facebook Page ──webhook POST (signed)──► aiohttp server (plugin)
   ▲                                          │ verify sig, dedup, allowlist
   │  Send API (Graph)                        ▼
   └────────────── MessengerAdapter ──► Hermes gateway ──► agent
```

Plugin dựng theo template plugin LINE bundled trong hermes-agent
(`plugins/platforms/line/`) — cùng mô hình webhook + HMAC verify + allowlist.

License: theo dự án của bạn.
