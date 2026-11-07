=================================================================================================
 PRJ18 - BÃI ĐỖ XE THÔNG MINH CHẠY TRÊN ESP32        (bản MicroPython / Python)
=================================================================================================
 BAO GỒM:
   2 đầu đọc RC522 (vào/ra), 2 servo barrier, LED xanh/đỏ, buzzer, cảm biến lửa,
   MQ-2, LCD I2C (tuỳ chọn), OLED SSD1306, web dashboard, WebSocket, lịch sử (flash), Telegram,
   đồng bộ giờ NTP, nạp code qua WiFi (WebREPL).

 GIAO TIẾP USB SERIAL VỚI HMI PYTHON (hmismart.py) :
   BAUD = 115200, chọn cổng COM của ESP32.
   Lệnh nhận: S, I (mở vào), i (đóng vào), O (mở ra), o (đóng ra), T<epoch>.

 SƠ ĐỒ CHÂN (giống bản Arduino)
   RC522 chung bus : SCK 18 | MISO 19 | MOSI 23        (cấp 3V3, KHÔNG cấp 5V)
   RC522 cổng vào  : SDA(SS) 5  | RST 4
   RC522 cổng ra   : SDA(SS) 26 | RST 25
   SERVO ĐIỀU KHIỂN TRỰC TIẾP: servo cổng vào GPIO13, servo cổng ra GPIO14
   LED xanh 27 | LED đỏ 33 (mỗi LED qua 220 ohm) | Buzzer 32
   Cảm biến lửa DO 34 | MQ-2 DO 35   (module báo mức THẤP khi phát hiện)
   I2C SDA 21 / SCL 22: LCD 16x2 (0x27) và OLED SSD1306 (0x3C)

 CÀI ĐẶT
   1. Nạp firmware MicroPython cho ESP32 (khuyên dùng v1.22 trở lên).
   2. Chép lên board: main.py (file này) và ssd1306.py (thư viện chính thức của MicroPython:
      mpremote mip install ssd1306).  Driver RC522 và LCD đã viết sẵn trong file này.
   3. Khởi động lại board là chạy.  Muốn đổi thông số, sửa phần CẤU HÌNH bên dưới.

 THỜI GIAN : có WiFi -> NTP; không WiFi -> bấm "ĐỒNG BỘ GIỜ" trên HMI.
   Khi chưa có giờ thật, ESP32 chạy giờ tạm tính từ lần cuối biết giờ. Nên đợi có giờ rồi mới quẹt thẻ.

 KHÁC BIỆT SO VỚI BẢN ARDUINO
   - Vòng lặp chính dùng uasyncio: các "waitMs" thành "await wait_ms", web/WebSocket/USB
     chạy song song trong lúc chờ (tương đương serviceNet() cũ).
   - Preferences  -> NVS (esp32.NVS).   LittleFS -> hệ thống file flash của MicroPython.
   - ArduinoOTA   -> WebREPL (mật khẩu 4-9 ký tự).   mDNS: tuỳ firmware, dùng hostname "parking".
=================================================================================================
"""
