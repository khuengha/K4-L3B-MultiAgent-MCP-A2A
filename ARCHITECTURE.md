# L3B Architecture Record

## Luồng xử lý

`solve_case` chạy pipeline xác định entity → điều phối các specialist → phát hiện
mâu thuẫn → áp dụng policy → verifier → output. Các specialist là các bước Python
xác định, không sử dụng LLM hoặc dịch vụ A2A bên ngoài. Nội dung khiếu nại chỉ được
đọc như dữ liệu; claim topic không được dùng làm bằng chứng kết luận.

## Trách nhiệm

- Entity agent dùng customer history và order để xác định một đơn duy nhất.
  Nhiều candidate cùng khớp sẽ trả ambiguous. Không coi claimed ID là bằng chứng.
- Order agent đọc item và product context khi scope yêu cầu.
- Shipment agent đối chiếu ngày giao hàng, shipping limits và shipment events.
- Payment agent đọc capture và refund lifecycle, dùng Decimal để tính tiền.
- Policy agent đọc policy đúng version của case và áp dụng rule theo issue đã phát hiện.
- Conflict agent phát hiện history/order không đồng nhất, item trùng khác nội dung,
  shipment events mâu thuẫn với summary. Không có precedence rõ ràng thì để unresolved.
- Verifier độc lập kiểm tra entity scope, evidence linkage, tổng refund và JSON Schema.
  Đây là kiểm tra bằng code trên evidence đã có, không phải một mô hình đánh giá độc lập.

Mỗi vai trò chỉ gọi nhóm tool tương ứng. Coordinator phát `task_assigned`; specialist
phát `tool_result_consumed` và `handoff`. Envelope trace mang case_id, actor, target,
evidence_refs và decision_code, không ghi suy luận nội bộ. CLI sở hữu case_received
và case_finalized; solve_case sở hữu các sự kiện ở giữa.

## Evidence và lỗi

Tool discovery được thực hiện trước khi điều tra. Gateway validate evidence schema;
workflow kiểm tra order/customer scope trong các row lồng nhau. Evidence ref giữ nguyên,
cache chỉ tồn tại trong một lần solve_case. Không tạo ID shipment/payment nếu nguồn
không cung cấp. Các tool lỗi/timeout/không tồn tại được ghi handoff và cache thất bại.

Giới hạn 20 call/case, tối đa 5 candidate order khi thiếu history, timeout 35 giây/call,
không retry tự động. Các bước chạy tuần tự, không tạo vòng lặp agent. History, policy,
order, items, shipment, payment timeline, refund timeline và product context tạo thành
8 call ở case thông thường có customer hint và product scope.

Nguồn thiếu hoặc mâu thuẫn dẫn tới needs_investigation, confidence thấp và không đề xuất
refund mới. Không coi lỗi refund tool là đã refund 0. Refund không vượt phần capture
còn lại; tổng refund_lines phải bằng recommended_refund_brl. Seller lấy từ entity đã
xác minh, không sao chép seller của một policy rule chung sang đơn khác.

## Giới hạn

Chưa có source precedence trong policy mẫu nên workflow không tự giải quyết các nguồn
mâu thuẫn. Không suy duplicate charge chỉ từ hai payment rows: cần sự kiện duplicate
được xác nhận. Evidence shipment thiếu sẽ không kết luận on_time. Product context
hiện được thu thập theo scope; chưa có quy tắc bồi thường riêng theo category.
Claim assessments hiện liên kết toàn bộ evidence của case, chưa tối ưu precision theo claim.
Các source có nhiều lifecycle records thiếu reference duy nhất vẫn cần điều tra thủ công.

## Tái lập và kiểm tra

Python >=3.11, dependency ranges trong pyproject.toml; không dùng model, random seed hay
API key bổ sung. Chạy `pytest -q tests/test_starter.py tests/test_workflow.py`,
`ruff check src tests`, sau đó `day09 run` và `day09 validate` khi muốn xử lý cả bộ case.
Test release_safety yêu cầu repo phát hành không chứa payload; nó không phù hợp với
workspace đã tải case-set.json và inputs. Không xóa dữ liệu học viên để làm test này pass.
