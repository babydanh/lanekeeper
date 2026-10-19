# lanekeeper

**Cho nhiều agent AI cùng sửa một repo một lúc mà không đè lên việc của nhau.**

Claude Code, Codex CLI, Gemini CLI, Cursor và omp mỗi agent làm trong một git worktree riêng ("làn"). lanekeeper theo dõi các làn và:

1. **Cảnh báo agent *trước khi* sửa đoạn code mà agent khác đang sửa**, kể cả phần chưa commit, kèm số dòng và tên hàm bao quanh.
2. **Gộp làn đã xong qua hàng đợi ở máy**: khoá, rebase, chạy kiểm tra, gộp thành 1 commit, push. Không cần Pull Request hay dịch vụ ngoài.
3. **Chặn lệnh git làm mất code** (`git stash`, `reset --hard`, `checkout .`, `clean -f`, …) trừ khi *bạn* cho phép.

Không phụ thuộc thư viện ngoài (thư viện chuẩn Python ≥ 3.11 + git). Chạy trên Windows, macOS, Linux. [English](README.md)

## Cài đặt

```bash
uv tool install lanekeeper        # hoặc: pipx install lanekeeper / pip install lanekeeper
lanekeeper install claude         # ghi hook vào ~/.claude/settings.json (gộp với hook sẵn có, có bản sao lưu)
lanekeeper install codex          # ~/.codex/hooks.json
lanekeeper install gemini         # ~/.gemini/settings.json
lanekeeper install cursor         # ~/.cursor/hooks.json
lanekeeper install omp            # chỉ chỗ chép adapters/omp/lanekeeper.ts
```

`--project` ghi vào cấu hình của repo thay vì của người dùng, `--dry-run` xem trước, `--uninstall` gỡ đúng phần của lanekeeper. Chạy `install` nhiều lần không bị nhân đôi hook.

## Dùng hằng ngày

```bash
git worktree add ../feat-a -b feat-a          # mỗi agent/việc một làn
# ... agent làm và commit thoải mái (commit WIP cũng được) ...

lk overlap .                                  # làn nào sẽ xung đột, ở đâu (cũng tự chạy sau mỗi lần sửa)
lk merge ../feat-a                            # rebase + kiểm tra, báo SẴN SÀNG — chưa push
lk merge ../feat-a --push                     # gộp: mỗi làn thành 1 commit trên main
lk merge . --train "feat-a feat-b feat-c" --push   # gộp nhiều làn một lượt, làn nào làm đỏ thì bị loại
lk                                            # ai đang làm gì
lk doctor                                     # sức khoẻ: lỗi hook, hook cũ, stash bị quên, việc dở
```

`lk` là tên tắt của `lanekeeper`.

## Cấu hình (tuỳ chọn)

File `.lanekeeper.toml` ở gốc repo, mọi khoá đều tuỳ chọn:

```toml
target = "main"                 # nhánh đích
protected = ["main", "master"]  # nhánh không bao giờ coi là làn
check = "npm test"              # kiểm tra nhanh, chạy trong làn trước khi giữ khoá
gate = "npx playwright test"    # kiểm tra nặng (E2E, CI từ xa...), chạy 1 lần mỗi lần gộp có --push
squash = true                   # mỗi làn thành 1 commit trên nhánh đích
```

Lệnh `gate` nhận biến `LANEKEEPER_BRANCH`, `LANEKEEPER_BASE`, `LANEKEEPER_HEAD`, `LANEKEEPER_CHANGED` (file đổi, mỗi dòng một file) để chỉ chạy test bị ảnh hưởng hoặc gọi CI rồi chờ. Gõ sai tên khoá là báo lỗi, không âm thầm tắt kiểm tra.

## Cho phép một lệnh bị chặn

Trong session đó, gõ đúng câu `cho phép git xoá` (hoặc `allow git discard`) làm prompt.

Chi tiết cách hoạt động, bảng hỗ trợ từng agent và so sánh với các dự án khác: xem [README tiếng Anh](README.md).

## Giấy phép

MIT
