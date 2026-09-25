# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả các quyết định kiến trúc có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý chính:

```text
Input Case
    │
    ▼
Entity Resolver
    │
    │ resolved / ambiguous / not_found
    ▼
Coordinator
    │
    ├──────────────► Order/Product Specialist
    │
    ├──────────────► Shipment Specialist
    │
    ├──────────────► Payment/Refund Specialist
    │
    └──────────────► Policy Specialist
                         │
                         ▼
                  Conflict Resolver
                         │
                         ▼
                     Verifier
                         │
                         ▼
                       Output
                         │
                         ▼
                       Trace

                 ┌──────────────┐
                 │ MCP Evidence │
                 └──────────────┘
                    ▲    ▲    ▲
                    │    │    │
              Specialists / Resolver / Verifier
```

## 2. Agent ownership

| Actor             | Input                                      | Trách nhiệm                                                           | Tool permission                                                             | Output/handoff                  |
| ----------------- | ------------------------------------------ | --------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------- |
| Entity/customer   | Case input, customer claims, candidate IDs | Xác định customer/order entity; đánh giá resolved/ambiguous/not_found | `get_order`, `get_customer_history` khi cần                                 | Entity resolution → Coordinator |
| Coordinator       | Case + entity result                       | Phân loại yêu cầu, điều phối specialist và quyết định tool cần thiết  | Điều phối, không tự động gọi mọi tool                                       | Specialist tasks                |
| Order/product     | Resolved order                             | Kiểm tra order status, order items và product context khi cần         | `get_order`, `get_order_items`, `get_product_context`                       | Order evidence                  |
| Shipment          | Resolved order + shipment-related task     | Phân tích shipping timeline, seller/logistics responsibility          | `get_shipment_summary`, `get_order_items`, `get_sellers` khi cần            | Shipment verdict                |
| Payment/refund    | Resolved order + payment-related task      | Đối chiếu payments, timeline và refund status                         | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` khi cần | Payment/refund verdict          |
| Policy            | Case context + verified facts              | Kiểm tra policy eligibility và resolution constraints                 | `get_policy`                                                                | Policy decision                 |
| Conflict resolver | Specialist results + evidence              | Phát hiện và xử lý mâu thuẫn giữa các nguồn                           | Evidence đã thu thập                                                        | Resolved/unresolved conflicts   |
| Verifier          | Final candidate output + evidence          | Kiểm tra invariants trước finalize                                    | Evidence đã thu thập; chỉ query bổ sung khi cần                             | PASSED / needs_investigation    |

Least privilege: Actor chỉ được sử dụng MCP tool cần thiết cho domain của mình. Việc tool tồn tại trong MCP discovery không có nghĩa actor được quyền gọi tool đó trong mọi case.

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

### Entity resolution

Candidate được kiểm tra bằng MCP thông qua `get_order`.

Quá trình resolution phân loại candidate thành:

- `resolved`: candidate được MCP xác nhận phù hợp.
- `ambiguous`: có nhiều candidate phù hợp nhưng chưa đủ evidence để phân biệt.
- `not_found`: không có candidate nào được MCP xác nhận.

Candidate bị reject nếu MCP không xác nhận entity hoặc dữ liệu không phù hợp với `case_id`.

Không tự động chọn candidate đầu tiên khi có nhiều candidate cùng phù hợp. Không tạo hoặc suy đoán entity ID.

### Confidence threshold

Entity chỉ được coi là `resolved` khi có evidence đủ để xác định entity. Trường hợp ambiguous hoặc thiếu evidence không được sử dụng để tạo verified conclusion.

Entity confidence không được sao chép trực tiếp thành final confidence của case. Final confidence phải phản ánh cả evidence completeness, consistency và mức độ chắc chắn của conclusion.

### A2A message envelope

Handoff giữa các agent sử dụng context tối thiểu:

```text
{
    case_id,
    task_type,
    entity_context,
    evidence_refs,
    required_action
}
```

## 4. Evidence và conflict lifecycle

MCP response được validate theo schema `mcp-evidence-response-v1` trước khi được sử dụng trong workflow.

Các điều kiện validation chính:

- Response phải thuộc đúng `case_id`.
- Evidence envelope phải hợp lệ.
- `evidence_ref` phải đúng format.
- `domain` phải thuộc các domain được định nghĩa.
- `result_hash` phải hợp lệ.
- Evidence không được thuộc case khác.

Sau khi MCP response được validate, workflow lưu và truyền `evidence_ref` cùng với kết quả tương ứng cho specialist và các bước xử lý tiếp theo.

Luồng evidence:

```text
MCP Request
    ↓
MCP Response
    ↓
Validate Evidence
    ↓
Check case_id / ownership
    ↓
Store evidence_ref
    ↓
Specialist consumes evidence
    ↓
Map evidence → claim/output
    ↓
Verifier
    ↓
Final Output
```

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout                |             2 retries | Nếu vẫn thất bại → không sử dụng evidence đó; chuyển sang `insufficient_evidence` hoặc `needs_investigation` | `tool_result_consumed`               |
| Entity not found/ambiguous |                     0 | Không suy đoán entity; giữ trạng thái `not_found`/`ambiguous` và hạ mức kết luận                             | `verification_completed`             |
| Source conflict            |                     0 | Ghi `data_conflicts`, giảm confidence; nếu ảnh hưởng conclusion → `needs_investigation`                      | `verification_completed`             |
| Invalid specialist result  | 0–1 nếu lỗi transient | Reject result; không đưa result không hợp lệ vào final conclusion                                            | `handoff` / `verification_completed` |


Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize output, Verifier thực hiện các kiểm tra sau:

### Schema validation

- Output phải validate theo `l3b-output-v2`.
- Đầy đủ các required fields.
- Không có field ngoài schema.
- Enum và data type phải hợp lệ.

### Entity scope

- Customer/order được sử dụng phải thuộc đúng `case_id`.
- Entity phải có trạng thái `resolved` trước khi đưa ra verified conclusion.
- Entity `ambiguous` hoặc `not_found` không được sử dụng để suy đoán conclusion.

### Rejected candidates

- Candidate bị reject không được sử dụng trong final output.
- Không tự động chọn candidate đầu tiên khi có nhiều candidate chưa được phân biệt.
- Không tạo hoặc suy đoán entity ID.

### Evidence ownership

- Mọi `evidence_ref` phải tồn tại trong evidence đã thu thập.
- Evidence phải thuộc đúng `case_id`.
- Không sử dụng evidence từ case khác.
- Evidence domain phải phù hợp với conclusion.

### Claim linkage

- Customer claim chỉ được xem là allegation.
- Claim chỉ được đánh giá là supported khi có evidence tương ứng.
- Claim không có supporting evidence không được biến thành verified finding.
- `claim_assessments` phải nhất quán với final conclusion.

### Timeline consistency

- Shipment conclusion phải phù hợp với shipment timestamps/events.
- Seller/logistics responsibility phải có evidence hỗ trợ.
- Các timeline conflict phải được ghi nhận trong `data_conflicts`.

### Payment/refund totals

- Payment analysis phải nhất quán với payment records.
- Payment total được đối chiếu với order/item amount khi cần.
- Valid split payment phải được phân biệt với duplicate charge.
- Duplicate charge phải có evidence tương ứng.
- Refund pending/failed phải có refund evidence.
- Policy eligibility không được xem là bằng chứng refund đã xảy ra.

### Source precedence

Khi nhiều nguồn có thông tin khác nhau, áp dụng thứ tự ưu tiên:

```text
Direct transaction evidence
        >
Derived evidence
        >
Customer allegation
```
