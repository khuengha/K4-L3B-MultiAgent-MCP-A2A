# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Resolve đúng order theo thứ tự ưu tiên (claimed trước), reject candidate lỗi; lấy customer history | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` → handoff sang domain specialists |
| Coordinator | Case JSON đầy đủ | Lập kế hoạch theo `investigation_scope` + claim topics; phân công specialist; tập hợp kết quả; dựng output | (không gọi tool trực tiếp) | Emit `task_assigned`; tổng hợp `assessment` |
| Order/product | `order_id` đã resolve | Đếm item/seller phục vụ `affected_entities`; phát hiện row trùng mâu thuẫn | `get_order_items`, `get_product_context`, `get_sellers` | `item_ids`, `seller_ids`, data conflicts |
| Shipment | `order_id`, mốc thời gian order | Xác định verdict giao hàng (`on_time`/`seller_delay`/`logistics_delay`/`lost`/`returned`/`conflicting`), seller trễ | `get_shipment_summary` | `shipment_analysis` |
| Payment/refund | `order_id`, timestamps order | Tính captured/refunded/refundable, chọn payment row theo timeline event khớp thời gian | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, `payment_references` |
| Policy | `policy_version`, primary_issue | Map issue → case_status/recommended_action/refund/responsible_parties từ policy rules (gọi đúng 1 lần) | `get_policy` | `financial_resolution`, `resolution_actions`, `root_cause_analysis` |
| Conflict resolver | Danh sách xung đột từ specialists | Ghi `data_conflicts` (field, ≥2 sources, selected_source, resolution_code), áp precedence | (không gọi tool) | Emit `policy_decided` |
| Verifier | Output nháp + evidence refs | Rà invariants (totals ≥ 0, confidence ∈ [0,1], status/refund/action nhất quán) trước finalize | (không gọi tool) | Emit `verification_completed` (PASS/FAIL) |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- **Xếp hạng candidate**: `claimed_order_id` (từ `customer_request`) đứng đầu, các `candidate_order_ids` còn lại giữ nguyên thứ tự. Candidate được thử tuần tự bằng `get_order`.
- **Accept/reject**: candidate bị reject khi (a) tool lỗi (không tồn tại trong domain này) hoặc (b) `order_id` trả về không khớp candidate. Candidate đầu tiên pass là order được chọn; các candidate đã reject ghi vào `rejected_candidates`.
- **Confidence**: baseline 0.9, trừ 0.15 mỗi rejected candidate, cộng 0.05 nếu khớp `claimed_order_id`, chặn trong [0.4, 0.99].
- **Message envelope (A2A nội bộ)**: các actor giao tiếp qua dict Python thuần, không network. Mỗi "message" là tuple (actor nguồn, actor đích, payload domain). Correlation dùng `case_id` truyền vào mọi MCP call và mọi trace event — không có conversation id riêng.
- **Handoff**: entity-agent emit `handoff` → `domain-specialists` sau khi resolve; mỗi specialist chỉ nhận payload domain của mình (least privilege). Không có vòng lặp: mỗi specialist chạy đúng 1 lần, kết quả thu về coordinator, không callback ngược.

## 4. Evidence và conflict lifecycle

- **Validate**: mọi MCP response được `EvidenceGateway.call()` validate qua `mcp-evidence-response-v1.schema.json` trước khi trả về; envelope hỏng ⇒ raise, được xử lý như "không có evidence" ở tầng workflow (không tự chế dữ liệu thay thế).
- **Lưu ref**: mỗi evidence thành công có `evidence_ref` ghi vào danh sách case (dedupe, tối đa 30) và emit ngay event `tool_result_consumed` với `tool_name` + `evidence_refs=[ref]` — đây là evidence-to-trace linkage.
- **Chọn nguồn theo precedence**: (1) timeline event khớp mốc thời gian của order (±30 ngày) > (2) row đầu tiên của listing. Mọi cặp giá trị mâu thuẫn (payment trùng `payment_sequential`, item trùng `order_item_id` khác thuộc tính, row lệch cửa sổ thời gian) đều ghi vào `data_conflicts` với ≥2 sources và `resolution_code`.
- **Unresolved conflict**: nếu precedence không phân định được, `selected_source` = `null` và `resolution_code` = `UNRESOLVED`; verdict domain tương ứng hạ xuống `conflicting`/`insufficient_evidence`.
- **Claim linkage**: mỗi claim trong `claim_assessments` tham chiếu evidence refs của domain liên quan (shipment/payment). Evidence không tái sử dụng giữa các case — danh sách refs bị reset mỗi case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/network | 1 lần, ngay lập tức, cùng arguments | Domain đó → verdict `insufficient_evidence`, không đoán dữ liệu | Không emit `tool_result_consumed`; ghi `policy_decided`/`NO_EVIDENCE` |
| Entity not found/ambiguous | 1 call/candidate, không retry | `entity_resolution.status=not_found`, output tối thiểu hợp lệ schema, không gọi domain tools | `policy_decided`/`ENTITY_NOT_FOUND` |
| Source conflict | Không retry (đọc lại cùng nguồn = phí budget) | Áp precedence, ghi `data_conflicts`, giữ verdict rõ ràng nhất | `policy_decided` với `resolution_code` |
| Invalid specialist result | Không retry | Bỏ qua kết quả, domain → `insufficient_evidence`; exception không thoát khỏi `solve_case` | `verification_completed`/`FAIL` |

- **Query budget**: mỗi case gọi tối đa 1 lần/tool cần thiết theo scope (customer history, product context chỉ khi `investigation_scope` bật). Không gọi tool cho domain không xuất hiện trong claims/scope. Không cache chéo case (evidence gắn case qua audit).
- **Idempotent**: các call đều là read-only; retry an toàn. Missing evidence không bao giờ được thay bằng dữ liệu phỏng đoán — chỉ hạ confidence/verdict.

## 6. Verification invariants

Trước khi emit `verification_completed`, verifier kiểm tra:

1. `case_id` output khớp case đang chạy (lỗi → 0 điểm hard gate).
2. Mọi evidence_ref trong output đúng pattern `ev_...`, có trong danh sách refs thu thập, và đã xuất hiện trong ≥1 `tool_result_consumed`.
3. `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`, `recommended_refund_brl`, mọi `amount_brl` ≥ 0 (nullable đúng chỗ cho phép).
4. `entity_resolution.status=resolved` ⇔ `resolved_order_ids` khác rỗng; `rejected_candidates` không chứa order đã resolve.
5. Refund/action nhất quán: `case_status=action_required` ⇒ có `resolution_actions`; `refund_pending` ⇒ không hành động hoàn tiền tức thời.
6. `confidence` của mọi khâu nằm trong [0, 1].
7. `claim_assessments.verdict` không mâu thuẫn với primary/secondary issues.

## 7. Reproducibility

- **Model/config**: pipeline thuần Python async (không LLM, không framework ngoài `mcp` SDK) — không có model inference, kết quả deterministic với cùng dữ liệu MCP server.
- **Dependencies**: pin trong `pyproject.toml` (`mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `httpx2>=2,<3`, `python-dotenv>=1.1,<2`); dev qua `.venv` và `pip install -e .`.
- **Concurrency**: chạy tuần tự từng case (`cli.py` for-loop) — 1 connection MCP duy nhất, không giới hạn thêm cần cấu hình.
- **Random seed**: không dùng randomness nghiệp vụ (trace `event_id`/`occurred_at` là duy nhất nhưng không ảnh hưởng output).
- **Lệnh chạy**: `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip` (dùng `.venv\Scripts\python.exe -m student_agent.cli` nếu script chưa trên PATH).
- **Giới hạn tài nguyên**: timeout MCP 300s/connection 30s (mặc định gateway); output mỗi case ≤ 30 evidence refs, ≤ 5 conflicts.
- **Bảo mật**: không ghi API key vào bất kỳ artifact; key chỉ đọc từ `.env` qua `Settings`.
