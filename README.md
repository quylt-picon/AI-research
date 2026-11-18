# Worker Report Live — Trợ lý giọng nói nhận báo cáo công việc (demo)

Demo cho **QNavi Approve Helper**: sau khi sửa xong, thợ gọi (bấm nút) và **nói** báo cáo.
Trợ lý (Gemini Live) đóng vai tổng đài viên: nghe, ghi vào phiếu, **hỏi lại những mục còn thiếu
theo bộ rule**, cho phép thợ **nói lý do** khi không cung cấp được, đọc lại tóm tắt để xác nhận,
rồi lưu báo cáo cho người duyệt.

> Vì sao làm vậy và vì sao chọn công nghệ này: xem [docs/phan-tich-nhu-cau.md](docs/phan-tich-nhu-cau.md).
> Tóm tắt: approve-helper phát hiện báo cáo thiếu/mơ hồ **sau khi** thợ nộp (case III/IV/VI/VIII) → phải trả về.
> Demo này hỏi thợ **ngay lúc báo cáo**, khi họ còn ở hiện trường.

## Chạy

```bash
uv sync --group dev
cp .env.example .env            # điền GEMINI_API_KEY (đã điền sẵn trong bản demo này)
uv run uvicorn app.main:app --reload --port 8100
# mở http://localhost:8100  (Chrome khuyến nghị; cần quyền micro; localhost được coi là "secure")
```

`uv run pytest` — 43 test, **không gọi Gemini** (Gemini được mock, giống cách approve-helper mock Typesense/LLM).

## 3 màn hình

| Tab | Ai dùng | Làm gì |
|---|---|---|
| **作業者** (Thợ) | Thợ — ưu tiên điện thoại | Chọn job → xem trước các mục sẽ bị hỏi → 🎙 bắt đầu → nói chuyện; checklist cập nhật realtime (✓ đủ / ○ thiếu / △ chưa cụ thể / ⏭ được miễn). Có ô gõ chữ dự phòng khi ồn. Cuộc gọi rớt → tự lưu nháp, gọi lại sẽ **tiếp tục** chứ không hỏi lại. |
| **承認者** (Người duyệt) | Người duyệt | Báo cáo đã nhận: 確認ポイント (cờ tự động từ rule), 3 trường QNavi `report_symptom/reason/content`, bảng mục, chi phí, transcript, payload sẵn cho approve-helper. |
| **ルール** (Rule) | Người quản lý rule | Xem rule, **tải lại** sau khi sửa file, xem **chính xác chỉ dẫn** server gửi cho Gemini với từng job. |

## Sửa rule

File duy nhất: [rules/report_rules.yaml](rules/report_rules.yaml) (đầu file có giải thích từng thuộc tính).
Sửa xong bấm **ルール再読み込み** ở tab ルール — không cần restart. File lỗi → server giữ bản cũ và báo lỗi cụ thể.

Ví dụ: thêm category 鍵 (khóa), riêng trường hợp 解錠 (mở khóa) phải hỏi người yêu cầu có xác nhận danh tính chưa:

```yaml
categories:
  鍵:
    vi: Khóa
    vocabulary: [シリンダー, 鍵穴, 解錠, 合鍵]
    fields:
      - key: key_type
        label: 鍵・シリンダーの種類
        type: text
        required: true
        waivable: true
        ask: 鍵の種類やシリンダーのメーカーはわかりますか？
    symptoms:
      - match: [解錠]
        fields:
          - key: identity_check
            label: 依頼者の本人確認
            type: text
            required: true
            waivable: false
            ask: 解錠の前に、依頼者の方の本人確認はされましたか？
```

Ai quyết định gì:
- **Code** (từ rule) quyết định mục nào thiếu / đủ / được miễn, và từ chối chốt báo cáo khi chưa đủ — nhất quán mọi cuộc gọi.
- **Gemini** chỉ hội thoại, trích xuất thông tin từ lời thợ, đánh giá lý do miễn có cụ thể không.

## Kiến trúc

```
Trình duyệt (mic 16kHz PCM) ──WS──► FastAPI /ws/report/{job} ──WS──► Gemini Live (gemini-3.8-live)
        ▲   loa 24kHz PCM, transcript,       │  giữ API key, xử lý tool call          │
        └── checklist realtime ◄─────────────┤                                         │
                                             │ update_report / set_cost_items /       │
                                             │ record_exemption / finalize_report ◄───┘
                                             ▼
                          ReportDraft  ◄── rules/report_rules.yaml
                          (chấm checklist bằng code)
                                             │
                     data/drafts/*.json (rớt cuộc gọi)   data/reports/*.json (đã chốt)
```

| File | Vai trò |
|---|---|
| `app/services/rules.py` | Đọc + kiểm tra YAML, ghép mục chung + theo category + theo triệu chứng |
| `app/services/report_draft.py` | Trạng thái báo cáo, **chấm checklist**, miễn, cờ cho người duyệt, map sang QNavi |
| `app/services/prompt.py` | Render rule + job → chỉ dẫn tiếng Nhật cho Gemini (bỏ comment và `vi:`) |
| `app/services/tools.py` | Khai báo function cho Gemini + xử lý; mọi kết quả đều kèm `remaining` + `next_hint` |
| `app/services/live_bridge.py` | Cầu nối trình duyệt ↔ Gemini Live, transcript, đếm token, giới hạn thời gian |
| `static/` | UI (HTML/CSS/JS thuần, không build) |
| `data/jobs.json` | Job mẫu — **dữ liệu giả** mô phỏng cấu trúc trouble của QNavi |

## Chi phí và dữ liệu

- Model mặc định `gemini-3.8-live` (đổi bằng `GEMINI_LIVE_MODEL`, dự phòng `gemini-2.5-flash-native-audio-preview-12-2025`).
- Giá (paid tier, trang pricing của Google 11/2026): audio vào $3/1M token (~$0.005/phút), audio ra $12/1M (~$0.018/phút), text vào $0.75/1M.
  Mỗi lượt hội thoại, toàn bộ ngữ cảnh (chỉ dẫn hệ thống ~8k token + audio/text của các lượt trước) được tính lại.
  **Đo thực tế (17/11/2026)**: test bằng text 80 giây, 6 lượt = **$0.094**
  (text vào 50k token — gần như toàn là chỉ dẫn hệ thống × 6 lượt; audio vào 9.8k — chính là giọng của trợ lý ở các lượt trước; audio ra 1.8k).
  Cuộc gọi giọng nói thật 3–5 phút ước tính **$0.2–0.4**. UI hiển thị chi phí ước tính theo `usage_metadata` thật.
  Đòn bẩy giảm chi phí lớn nhất: rút gọn chỉ dẫn hệ thống (bớt ví dụ/câu hỏi mẫu trong rule).
- Giới hạn cứng `MAX_SESSION_SECONDS` (mặc định 480 giây). Còn 1 phút thì trợ lý được nhắc kết thúc.
- **Free tier** của Gemini API miễn phí nhưng Google có thể dùng nội dung để cải thiện sản phẩm → **không dùng dữ liệu thật của khách hàng** khi key đang ở free tier.
- API key chỉ nằm trong `.env` ở server (không gửi xuống trình duyệt; có test kiểm tra điều này).

### Test thật với Gemini (tốn phí)

```bash
uv run python scripts/smoke_test_live.py            # đóng vai thợ bằng text, J-1001, kết quả lưu vào thư mục tạm
uv run python scripts/smoke_test_live.py --save     # lưu vào data/reports để xem ở tab 承認者
```
Giới hạn mặc định: 8 lượt thợ, 150 giây (lần chạy ngày 17/11 hết 80 giây, $0.094).

## Nối vào approve-helper sau này

- Cùng stack (Python 3.12, FastAPI, uv, cấu trúc `app/core`, `app/services`) → chuyển thành router `/api/v1/worker-report` được.
- Mỗi báo cáo đã có `approve_helper_payload` đúng định dạng `POST /api/v2/estimation-evaluation` (`large`, `description`, `items[{item_label, price, quantity}]`, `price` = tổng tiền dòng).
- Rule chi phí dùng cùng ngưỡng `ITEM_PRICE_THRESHOLDS` và cùng ý nghĩa case III/IV/VIII của `overall_evaluation_service.py`.

## Giới hạn của demo

- Chưa có đăng nhập / phân quyền; job lấy từ file JSON.
- Chưa nhận ảnh qua camera (Live API hỗ trợ, nhưng phiên audio+video chỉ 2 phút).
- Chưa dùng session resumption của Live API; mất mạng thì kết thúc cuộc gọi và lưu nháp, gọi lại sẽ tiếp tục.
- Tiếng ồn hiện trường: nên dùng tai nghe; echo cancellation của trình duyệt đã bật.
