# Phân tích nhu cầu: Trợ lý giọng nói nhận báo cáo công việc của thợ

> Mục tiêu tài liệu: xuất phát từ **vai trò và nhu cầu thật** trong QNavi / Approve Helper,
> sau đó mới chọn công nghệ. Gemini Live chỉ là công cụ được chọn vì nó đáp ứng nhu cầu,
> không phải điểm xuất phát.

---

## 1. Bối cảnh trong hệ thống hiện tại

Luồng hiện tại của QNavi (suy ra từ dữ liệu và code của approve-helper):

```
Cửa hàng (店舗)            QNavi                     Thợ / nhà thầu (作業者)        Người duyệt (承認者)
─────────────────         ─────────────────         ───────────────────────        ───────────────────────
Tạo yêu cầu sửa chữa  ─►  Phân loại issue        ─►  Đến hiện trường, sửa      ─►  Duyệt báo cáo + báo giá
 description               (large / small)            Báo cáo:                      (approve-helper
 large/small category                                  report_symptom  現状          /estimation-evaluation)
                                                       report_reason   原因
                                                       report_content  作業内容
                                                       + các dòng báo giá (items)
```

Approve-helper đánh giá báo giá **sau khi** thợ đã nộp, và đưa ra các case
(`app/services/overall_evaluation_service.py`). Rất nhiều case thực chất là
**"báo cáo thiếu/không rõ → phải trả về cho thợ"**:

| Case approve-helper | Ý nghĩa | Hành động hiện tại |
|---|---|---|
| VI `UNCLEAR_TROUBLE` | Mô tả sự cố không rõ | Yêu cầu sửa mô tả |
| IV `UNCLEAR_ITEMS` | Tên item mơ hồ ("AAA", "一式") | **Không duyệt**, bảo thợ đổi tên item |
| III `MISSING_CORE` | Không tách 材料費 / 作業費 | Bảo thợ tách chi tiết |
| V `MISMATCHED_ITEMS` | Item không khớp sự cố | Kiểm tra lại |
| VIII `PRICE_ANOMALY` | Giá lệch chuẩn | Cần **căn cứ đơn giá** |

→ **Vấn đề gốc**: thông tin thiếu được phát hiện quá muộn. Mỗi lần trả về là một vòng
liên lạc (thợ phải nhớ lại công việc đã làm vài ngày trước), chậm duyệt, chậm thanh toán.

**Cơ hội**: hỏi thợ **ngay lúc báo cáo**, khi họ còn đứng ở hiện trường và nhớ rõ —
giống một tổng đài viên có checklist trong tay.

---

## 2. Các vai trò và nhu cầu

### 2.1 Thợ (作業者) — người dùng chính

| Tình huống thực tế | Nhu cầu |
|---|---|
| Vừa sửa xong, tay bẩn, đang đứng ở bếp / mái nhà / bãi đỗ xe | **Rảnh tay**: nói thay vì gõ |
| Môi trường ồn (quạt hút, máy nén) | Có **chữ hiển thị** song song để kiểm tra lại; có ô **gõ chữ** dự phòng |
| Không giỏi viết báo cáo, hay viết cụt ("修理しました") | Được **hỏi gợi ý** cụ thể, không phải tự nghĩ ra cần viết gì |
| Thời gian ít, còn việc khác | Hỏi **từng câu một, ngắn**; **không hỏi lại** những gì hệ thống đã biết (cửa hàng, nội dung yêu cầu, loại sự cố) |
| Có những thông tin không lấy được (tem máy mờ, cửa hàng cấm chụp ảnh…) | Được **nói lý do** để bỏ qua mục đó, và lý do được ghi nhận — không bị kẹt |
| Sợ bị trả hồ sơ | Biết rõ **còn thiếu gì**, và cuối cuộc gọi được **đọc lại tóm tắt** để xác nhận |

### 2.2 Người duyệt (承認者 / vận hành QNavi)

| Nhu cầu | Đáp ứng bằng |
|---|---|
| Báo cáo đủ 3 trường chuẩn `report_symptom / report_reason / report_content` | Output map thẳng vào 3 trường này |
| Biết mục nào **bị bỏ qua và vì sao** | Danh sách *exemption* kèm lý do + đánh dấu lý do có cụ thể hay không |
| Item báo giá có tên rõ, tách 材料費/作業費, giá cao có căn cứ | Rule kiểm tra item (cùng logic với case III/IV/VIII) |
| Có bằng chứng khi tranh chấp | Lưu **transcript** cả cuộc hội thoại |
| Nguyên nhân do con người/tai nạn → có thể liên quan **bảo hiểm** (保険 là một category riêng) | Rule điều kiện: nếu nguyên nhân là 人為的・事故 thì hỏi thêm chi tiết |

### 2.3 Người quản lý rule (vận hành / admin)

| Nhu cầu | Đáp ứng bằng |
|---|---|
| Thêm / sửa "loại sự cố X thì cần những gì" **không cần sửa code** | 1 file YAML: `rules/report_rules.yaml` |
| Đọc hiểu được, có chú thích | YAML cho phép comment; có trường `vi:` giải thích tiếng Việt |
| Rule áp dụng **giống nhau** cho mọi cuộc gọi | Code kiểm tra đủ/thiếu (deterministic), không phó mặc cho LLM |

### 2.4 Hệ thống (QNavi / approve-helper)

- Dữ liệu ra phải **cấu trúc hoá**, đúng tên trường hiện có, tiếng Nhật.
- Sau này có thể gọi thẳng `/api/v2/estimation-evaluation` với `description` + `items` đã thu được.
- API key không được lộ ra phía client.

---

## 3. Từ nhu cầu → quyết định thiết kế

| Nhu cầu | Quyết định | Vì sao không chọn cách khác |
|---|---|---|
| Rảnh tay, nói tự nhiên, cắt lời được | **Gemini Live** (voice-to-voice realtime, native audio) | STT → LLM → TTS từng lượt: chậm, không cắt lời được, cảm giác "máy" |
| Kiểm tra đủ/thiếu phải nhất quán | LLM chỉ **hội thoại + trích xuất**, gọi tool `update_report`; **code** chấm checklist theo rule và trả về mục còn thiếu | Để LLM tự nhớ checklist → dễ bỏ sót, mỗi lần mỗi khác (giống triết lý approve-helper: *"Code decides the case"*) |
| LLM không được tự "chốt" báo cáo | Tool `finalize_report` **từ chối** nếu còn mục bắt buộc chưa có/chưa được miễn, hoặc thợ chưa xác nhận | — |
| Thợ nói lý do để bỏ qua | Tool `record_exemption`; rule quy định mục nào được miễn (`waivable`) | Mục cốt lõi (現状, 作業内容, 作業結果) **không** được miễn |
| Audit | Bật `input/output_audio_transcription` của Live API, lưu transcript | — |
| Bảo mật key | **Backend proxy** (FastAPI WebSocket) giữ key; trình duyệt chỉ nói chuyện với backend | Ephemeral token cũng được nhưng tool call vẫn phải xử lý ở server (đọc rule, lưu báo cáo) → proxy đơn giản hơn |
| Dễ gộp vào approve-helper sau này | Cùng stack **Python 3.12 + FastAPI + uv**, cấu trúc `app/core`, `app/services` | — |
| Demo nhanh, không đụng dữ liệu thật | Lưu **JSON file** (`data/jobs.json`, `data/reports/*.json`); job mẫu là **dữ liệu giả** mô phỏng cấu trúc thật | Không kết nối Typesense / DB thật |
| Từ ngữ chuyên ngành (室外機, 型番, グリストラップ…) | Đưa từ vựng trong rule vào `custom_vocabulary` của transcription | — |

### Vì sao dùng YAML cho rule

- **Người** đọc/sửa được (có comment, ít dấu ngoặc hơn JSON).
- **Code** parse được để kiểm tra đủ/thiếu.
- **Gemini** không đọc YAML thô; server **render** rule thành chỉ dẫn tiếng Nhật gọn gàng
  (danh sách mục, câu hỏi mẫu, tiêu chí chất lượng) → LLM hiểu tốt nhất,
  và comment nội bộ không bị gửi đi.

---

## 4. Luồng một cuộc báo cáo

```
Thợ chọn job ─► Bấm "Bắt đầu báo cáo"
                     │
                     ▼
   Trợ lý chào, nhắc lại job (đã biết), hỏi mở: "Hiện trạng thế nào, đã làm gì?"
                     │
          Thợ kể tự nhiên (1 lần nói được nhiều ý)
                     │
                     ▼
   LLM trích xuất → update_report / set_cost_items ─► Code chấm rule ─► trả "còn thiếu: …"
                     │                                                        │
                     ◄────────────────────────────────────────────────────────┘
                     │
   Hỏi từng mục còn thiếu ──► Thợ trả lời ──► cập nhật
                     │
                     ├─ Thợ: "Không xem được vì …" ─► record_exemption (nếu mục cho phép miễn)
                     │                                  mục không được miễn → giải thích, gợi ý "ước lượng cũng được"
                     ▼
   Checklist "ready" ─► Đọc lại tóm tắt (số liệu phải đọc lại) ─► Thợ xác nhận
                     │
                     ▼
   finalize_report ─► lưu data/reports/<id>.json ─► hiện ở màn hình Người duyệt
```

## 5. Ngoài phạm vi demo (để sau)

- Nhận ảnh qua camera (Live API hỗ trợ video 1 fps, nhưng phiên audio+video chỉ 2 phút).
- Tự gọi `/estimation-evaluation` của approve-helper ngay sau khi chốt báo cáo.
- Đăng nhập, phân quyền, lưu DB thật, đồng bộ QNavi.
- Nối lại phiên (session resumption) khi mạng chập chờn.
