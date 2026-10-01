# ⚡ VaultBox Server Tracking (Web Dashboard)

Trang web theo dõi trực tiếp trạng thái và log của tất cả các máy chủ **Google Colab, Kaggle, và Local Worker** đang hoặc đã kết nối tới Cloudflare Relay Server của VaultBox.

---

## 🌟 Tính Năng Nổi Bật
1. **Theo dõi đa máy chủ (Multi-Server Tracking)**:
   - Hiển thị danh sách toàn bộ server đang online 🟢 hoặc offline trong vòng 12 tiếng qua ⚪.
   - **Tự động lọc**: Server đã ngắt kết nối quá **12 tiếng** sẽ tự động bị ẩn và xóa khỏi hệ thống.
   - Hiển thị môi trường: `Google Colab`, `Kaggle`, `Local PC`.
2. **Chi tiết Job & Tiến trình (Transfer & Optimize Image)**:
   - Thanh tiến trình 4 giai đoạn: `Download` ➔ `Extract / Optimize` ➔ `Upload`.
   - Thông số thời gian thực: Tốc độ (MB/s), Dung lượng đã tải (MB/GB), % tiến trình.
   - Hàng đợi file (Item Queue): Xem trạng thái từng file (`Pending`, `Active ⏳`, `Done ✅`, `Skipped ⏭️`) kèm thời gian xử lý chi tiết (vd: `2.4s`, `420ms`).
   - Bảng so sánh dung lượng ảnh trước và sau khi tối ưu hóa.
3. **Terminal Console Xem Log Trực Tiếp (Y chang Colab)**:
   - Tô màu cú pháp thông minh: Thành công (xanh lá), Lỗi (đỏ), Cảnh báo (vàng), Tiến trình (xanh dương).
   - Tự động cuộn (`Auto-Scroll`), Tìm kiếm lọc log theo từ khóa.
   - Nút `Copy Log`, `Export Log (.log)`, và chế độ `Fullscreen`.
4. **Không cần mật khẩu**: Truy cập nhanh mọi lúc, mọi nơi trên máy tính, điện thoại, tablet.

---

## 🚀 Cách Chạy Web

### 1. Chạy trên máy tính cá nhân (Local)
- Nhấp đúp chuột vào file **`runWeb.bat`**.
- Trình duyệt sẽ tự động mở trang web tại địa chỉ: `http://localhost:5173`.

### 2. Deploy lên Vercel (Để xem trên điện thoại / bất kỳ đâu)
1. Đẩy thư mục `Web Tracking Vaultbox` lên GitHub repository của bạn (hoặc import trực tiếp vào Vercel).
2. Trên [Vercel Dashboard](https://vercel.com/):
   - Chọn **Add New Project** ➔ Chọn repo `Web Tracking Vaultbox`.
   - Không cần cấu hình gì thêm (Zero-Config), bấm **Deploy**.
3. Bạn sẽ nhận được link web dạng `https://vaultbox-server-tracking.vercel.app` để xem log mọi lúc mọi nơi 24/7!
