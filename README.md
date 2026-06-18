<p align="center">
  <img src="https://user3223.na.imgto.link/public/20260529/logo-with-text.avif" alt="PikPak Manager Premium" width="300">
</p>

<h1 align="center">PikPak Manager Premium</h1>

[![Premium](https://img.shields.io/badge/License-Premium-gold?style=for-the-badge)](https://github.com/CeatursHarmginton/pikpak-manager-premium)
[![Version](https://img.shields.io/badge/Version-1.1.0-blue?style=for-the-badge)](https://github.com/CeatursHarmginton/pikpak-manager-premium)
[![Platform](https://img.shields.io/badge/Platform-Windows%20Portable-brightgreen?style=for-the-badge)](https://github.com/CeatursHarmginton/pikpak-manager-premium)

**PikPak Manager Premium** là một ứng dụng khách (Client) chạy cục bộ trên máy tính được thiết kế dành riêng cho dịch vụ lưu trữ đám mây PikPak. Ứng dụng mang đến trải nghiệm quản lý tệp tin trực quan như một phần mềm Desktop chuyên nghiệp, đồng thời tối ưu hóa vượt trội tốc độ tải xuống, truyền tải hình ảnh và khả năng stream video chất lượng cao mà trình duyệt thông thường không thể đạt được.

---

## 🚀 Các Tính Năng Nổi Bật

### 💻 Giao Diện Desktop Hiện Đại & Trực Quan
*   **Trình quản lý tệp tin chuyên nghiệp**: Duyệt thư mục nhanh chóng, mượt mà với đầy đủ các chế độ hiển thị dạng Lưới (Grid) và Danh sách (List).
*   **Xem trước thông minh (Media Preview)**: Xem nhanh ảnh, tài liệu và phát video trực tiếp ngay trong giao diện mà không cần tải về.
*   **Hàng đợi tải xuống (Download Queue)**: Quản lý các tiến trình tải tệp cục bộ trực quan, hỗ trợ tạm dừng (Pause) và tiếp tục (Resume) tải các tệp tải dở dang một cách thông minh.

### ⚡ Xem Ảnh & Thumbnail Siêu Tốc (Parallel Image Streaming)
*   **Công nghệ tải ảnh song song (Parallel Image Proxy)**: Chia nhỏ hình ảnh chất lượng cao thành các phân đoạn 256 KB và tải song song qua tối đa 64 kết nối đồng thời từ các máy chủ CDN của PikPak, giúp ảnh hiển thị gần như ngay lập tức.
*   **Hệ thống bộ nhớ đệm 3 lớp (LRU Cache)**:
    *   **Thumbnail Cache (LRU 256MB)**: Tải cực nhanh ảnh thu nhỏ của tệp tin.
    *   **Segment Cache (LRU 2GB)**: Lưu các phân đoạn ảnh 256KB giúp tiếp tục tải phần còn thiếu nếu bị gián đoạn.
    *   **Full Image Cache (LRU 1GB)**: Cache hình ảnh đầy đủ trong 6 giờ, dùng chung cache cho các tệp trùng lặp nhờ cơ chế check hash nội dung.
*   **Tự động tải trước (Warmup Queue)**: Tự động tải trước tối đa 160 ảnh thu nhỏ và 80 ảnh đầy đủ ở các thư mục đang xem hoặc chế độ Rạp hát (Theater Mode) để mang lại trải nghiệm lướt ảnh không độ trễ.
*   **Đánh giá sức khỏe CDN (Host Health Check)**: Hệ thống tự động theo dõi độ trễ và tỷ lệ lỗi của các CDN PikPak để ưu tiên các máy chủ nhanh nhất và tạm dừng các máy chủ đang gặp sự cố.

### 🎬 Trình Phát Video Thông Minh (Smart Video Player)
*   **Tối ưu luồng phát**: Tự động ưu tiên đường truyền video gốc chất lượng cao trực tiếp, tự động chuyển sang HLS (`web-vod-xdrive`) khi cần thiết để đảm bảo video chạy mượt mà nhất.
*   **Chọn chất lượng linh hoạt**: Cho phép người dùng chuyển đổi các độ phân giải video sẵn có từ PikPak và ghi nhớ cấu hình chất lượng yêu thích cho các lần phát tiếp theo.

### 🔐 Két Sắt Bảo Mật Premium (Secure Vault)
*   **Hỗ trợ thư mục Két sắt (Vault)**: Tích hợp đầy đủ các chức năng bảo mật cao của két sắt PikPak (`/api/vault/*`), mã hóa và bảo vệ các thư mục nhạy cảm cá nhân của bạn (yêu cầu bản quyền Premify hợp lệ).

### ☁️ Tải Trực Tiếp Về Google Drive qua Google Colab
*   **Đồng bộ đám mây không tốn băng thông**: Kết nối trực tiếp đến môi trường Google Colab để tải tệp từ PikPak thẳng về Google Drive của bạn mà không tốn dung lượng ổ cứng hay băng thông mạng của máy tính cá nhân.

---

## 🔑 Hướng Dẫn Nhập Tài Khoản & Đăng Nhập

PikPak Manager Premium hỗ trợ rất nhiều phương thức đăng nhập linh hoạt để tối ưu hóa sự tiện lợi và tính bảo mật cho người dùng:

### 1. Các Phương Thức Nhập Tài Khoản

| Phương thức | Mô tả | Mức độ khuyên dùng | Ưu điểm |
| :--- | :--- | :--- | :--- |
| **Trợ lý Trình duyệt<br>(Browser Login Helper)** | Mở trang chủ PikPak trên trình duyệt hiện tại của bạn. Sau khi đăng nhập, hệ thống tự liên kết phiên làm việc. | ⭐⭐⭐⭐⭐ *(Khuyên dùng)* | **Cực kỳ an toàn**, không cần chia sẻ mật khẩu của bạn trực tiếp với ứng dụng. |
| **Email & Mật khẩu** | Nhập trực tiếp Email/Số điện thoại và mật khẩu tài khoản PikPak của bạn. | ⭐⭐⭐⭐ | Tiện lợi, hỗ trợ tự động đăng nhập lại hoàn toàn từ mật khẩu khi hết hạn phiên sâu. |
| **Mã Token Mã hóa<br>(Encoded Token)** | Dán chuỗi mã khóa token đã được mã hóa sẵn. | ⭐⭐⭐ | Nhanh gọn, thích hợp khi cần đăng nhập nhanh tài khoản phụ. |
| **Cặp Token truy cập<br>(Access & Refresh Token)** | Nhập thủ công cặp Access Token và Refresh Token của tài khoản. | ⭐⭐⭐ | Phù hợp cho nhà phát triển hoặc khi cần debug chuyên sâu. |
| **Xuất phiên làm việc<br>(Exported Session)** | Dán chuỗi JSON Session/localStorage được xuất ra từ PikPak bản Web. | ⭐⭐⭐⭐ | Vượt qua các bước captcha xác thực ban đầu một cách nhanh chóng. |

---

### 🔄 Cơ Chế Tự Động Duy Trì Phiên Đăng Nhập Thông Minh

Để đảm bảo các tác vụ tải xuống chạy ngầm hoặc truyền tải dữ liệu không bị gián đoạn, ứng dụng tích hợp một **vòng lặp kiểm tra token chạy ngầm (Token Maintenance Loop)** cực kỳ mạnh mẽ:
*   **Tự động làm mới**: Cứ mỗi 5 phút, ứng dụng sẽ quét toàn bộ tài khoản và tự động làm mới (refresh token) cho các tài khoản có phiên làm việc sắp hết hạn trong vòng 45 phút tiếp theo.
*   **Tự động đăng nhập lại**: Nếu Refresh Token bị lỗi do hết hạn sâu, ứng dụng sẽ sử dụng thông tin đăng nhập (nếu đăng nhập bằng Email/Password) để tiến hành đăng nhập lại hoàn toàn tự động.
*   **Hệ thống Bảo vệ Tài khoản (Safety Guard)**: Tránh việc tài khoản bị khóa do hệ thống gửi yêu cầu liên tục khi gặp lỗi. Nếu PikPak yêu cầu Captcha, xác minh danh tính hoặc báo Rate Limit, ứng dụng sẽ tự động kích hoạt **chế độ chờ (cooldown/backoff)** riêng cho tài khoản đó và giới hạn đăng nhập lại bằng mật khẩu tối đa **3 lần/ngày**.

---

## 💻 Hướng Dẫn Sử Dụng Nhanh

### 1. Sử dụng phiên bản Portable chuyên dụng (Dành cho người dùng)
Ứng dụng được đóng gói dưới dạng **Portable (.exe) duy nhất chạy trực tiếp không cần cài đặt**:
1.  Tải về tệp tin `PikPak Manager.exe`.
2.  Nhấp đúp chuột để khởi chạy ứng dụng.
3.  Ứng dụng sẽ tự động khởi động một Web Server cục bộ tại cổng `8765` và hiển thị giao diện Desktop mượt mà.
4.  **Tính năng khay hệ thống (System Tray)**: Khi bạn nhấn nút Đóng (X) trên cửa sổ, ứng dụng sẽ không tắt mà thu nhỏ xuống khay hệ thống của Windows để tiếp tục chạy các tác vụ tải ngầm và duy trì token tài khoản.
    *   *Nhấp chuột phải vào biểu tượng khay hệ thống* để: Mở giao diện, Dừng Server, Khởi động lại Server hoặc Thoát hoàn toàn ứng dụng.

> [!IMPORTANT]
> **Bảo mật dữ liệu của bạn**: Toàn bộ tài khoản, phiên làm việc, caches và cấu hình được lưu trữ hoàn toàn cục bộ trong thư mục `data/` nằm ngay bên cạnh tệp tin `.exe` (trong cơ sở dữ liệu SQLite mã hóa `pikpak_manager.sqlite3`). Ứng dụng **không bao giờ** gửi thông tin tài khoản của bạn lên bất kỳ máy chủ trung gian nào. Khi cập nhật phiên bản mới, bạn chỉ cần thay thế file `.exe` mới và giữ lại thư mục `data/` cũ để giữ nguyên tất cả tài khoản và dữ liệu.

---

### 2. Hướng dẫn cấu hình Google Colab để tải về Google Drive
Để tải tệp cực tốc thẳng về Google Drive cá nhân của bạn:
1.  Mở tệp notebook `colab/PikPak_Manager_Colab_Worker.ipynb` trong tài khoản Google Colab của bạn.
2.  Tải lên hoặc dán mã nguồn `colab/colab_worker.py` vào thư mục làm việc `/content/colab_worker.py` trên Colab.
3.  Chạy các dòng lệnh trong notebook để kết nối (mount) Google Drive của bạn và sinh ra một **API Token**.
4.  Sao chép **Worker URL** (được cung cấp qua tunnel hoặc proxy công khai của Colab), **API Token**, và **đường dẫn lưu Drive** mong muốn.
5.  Mở mục **Cài đặt (Settings)** trong phần mềm PikPak Manager Premium và dán các thông tin trên vào.
6.  Bây giờ, bạn chỉ cần nhấp chuột phải vào bất kỳ tệp tin nào trên danh sách và chọn **"Send to Colab"** để tiến hành tải thẳng về Drive một cách thần tốc!

---

## 📄 License & Integrity
Developed with a modular design separating the FastAPI backend logic from the HTML5 desktop frontend. Feel free to contribute or report issues in the repository tracker!
