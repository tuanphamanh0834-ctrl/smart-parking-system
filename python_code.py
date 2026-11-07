import sys
import time
import os
import gc
import json
import struct
import select
import binascii
import hashlib
import framebuf
import machine
import network
import esp32
import uasyncio as asyncio
from machine import Pin, I2C, SPI, PWM

# ================================== CẤU HÌNH ==================================
WIFI_SSID = "TEN_WIFI"             # WiFi nhà/xưởng -> ĐIỀN TÊN WIFI CỦA BẠN
WIFI_PASS = "MAT_KHAU_WIFI"        # -> ĐIỀN MẬT KHẨU WIFI
AP_SSID = "PUCZUU"               # ESP32 luôn phát thêm WiFi riêng để dự phòng
AP_PASS = "12345678"             # (192.168.4.1)
HOSTNAME = "parking"             # http://parking.local (nếu firmware hỗ trợ mDNS)
OTA_PASS = "ota12345"            # mật khẩu WebREPL (4-9 ký tự) -> NÊN ĐỔI
WEB_PIN = "1234"                 # PIN khi mở barrier / reset từ web  -> NÊN ĐỔI
GMT_OFFSET = 7 * 3600            # Việt Nam UTC+7
TG_TOKEN = ""                    # Telegram bot token (để trống = tắt)
TG_CHAT = ""                     # Telegram chat id

USE_LCD = True                   # True = có LCD I2C 16x2 (0x27). Cần module đổi mức 3.3V<->5V cho SDA/SCL
HTTP_PORT = 80
WS_PORT = 81
OLED_ADDR = 0x3C
LCD_ADDR = 0x27

PIN_SCK, PIN_MISO, PIN_MOSI = 18, 19, 23
PIN_SS_IN, PIN_RST_IN = 5, 4
PIN_SS_OUT, PIN_RST_OUT = 26, 25
PIN_SERVO_IN = 13                # Tín hiệu servo barrier cổng vào
PIN_SERVO_OUT = 14               # Tín hiệu servo barrier cổng ra
PIN_LED_GREEN = 27
PIN_LED_RED = 33
PIN_BUZZER = 32
PIN_FLAME = 34
PIN_MQ2 = 35
BUZ_ON = 1                       # buzzer kích mức thấp thì đổi thành 0 (và BUZ_OFF thành 1)
BUZ_OFF = 0
# ==============================================================================

EPOCH_DELTA = 946684800          # MicroPython tính giờ từ năm 2000, Unix từ 1970

MASTER_UID = 0x779A2306
MAX_CARS = 4
MAX_VIP_SIZE = 2
FEE_PER_UNIT = 1                 # đồng / giây
BARRIER_DELAY = 3000
OPEN_ANGLE = 0
CLOSE_ANGLE = 90

HISTORY = "/history.csv"
HISTORY_OLD = "/history_old.csv"

# ----- Bản chụp trạng thái dùng chung cho HMI, web và OLED -----
slots = [["", 0, False] for _ in range(4)]      # [uid, ts, vip]
st_parked = st_vip = 0
st_max = 4
gate_in = gate_out = fire = gas_a = admin = False
rtc_unix = 0
rtc_at = 0
last_stat = ""
last_event = "San sang"
pending_tg = ""
cnt_in = cnt_out = cnt_rev = 0
cnt_day = -1
need_count = False
usb_buf = ""

# ----- Trạng thái bãi xe -----
parked_uids = [0] * MAX_CARS
parked_ts = [0] * MAX_CARS
vip_list = [0] * MAX_VIP_SIZE
parked_count = vip_parked_count = vip_count = 0
gas_warn = busy = False
# Mỗi cổng có trạng thái giữ mở thủ công riêng; không lưu qua khởi động lại.
manual_hold_in = manual_hold_out = False
gate_in_open = gate_out_open = fire_on = False
clock_t0 = 1700000000
t_time = t_stat = t_save = 0

# ----- Thiết bị (được tạo trong core_init / main) -----
sta = ap = i2c = oled = lcd = spi = None
rfid_in = rfid_out = servo_in = servo_out = None
oled_ok = False
ota_on = False
ws_clients = {}
WS_MAX = 5
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

LED_GREEN = Pin(PIN_LED_GREEN, Pin.OUT, value=0)
LED_RED = Pin(PIN_LED_RED, Pin.OUT, value=0)
BUZZER = Pin(PIN_BUZZER, Pin.OUT, value=BUZ_OFF)
FLAME = Pin(PIN_FLAME, Pin.IN)
MQ2 = Pin(PIN_MQ2, Pin.IN)


# ================================== TIỆN ÍCH ==================================
def millis():
    return time.ticks_ms()


def since(t):
    return time.ticks_diff(time.ticks_ms(), t)


def to_int(s):                                  # giống String::toInt() của Arduino
    s = s.strip()
    i, n = 0, len(s)
    if i < n and s[i] in "+-":
        i += 1
    while i < n and s[i] in "0123456789":
        i += 1
    try:
        return int(s[:i])
    except ValueError:
        return 0


def unix_utc():
    return int(time.time()) + EPOCH_DELTA


def time_valid():
    return unix_utc() > 1700000000


def local_epoch():
    return unix_utc() + GMT_OFFSET


def now_str():
    if not time_valid():
        return "t+%ds" % (millis() // 1000)
    t = time.gmtime(time.time() + GMT_OFFSET)
    return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


def log_exc(e):
    try:
        sys.print_exception(e)
    except AttributeError:
        print("ERR", repr(e))


def emit(line):                                 # gửi 1 dòng ra USB + mọi client WebSocket
    print(line)
    ws_broadcast(line)


# ================================== LƯU TRỮ (NVS) ==================================
class Prefs:                                    # tương đương Preferences của Arduino
    def __init__(self, ns):
        self.nvs = esp32.NVS(ns)

    def get_int(self, k, d=0):
        try:
            return self.nvs.get_i32(k)
        except OSError:
            return d

    def put_int(self, k, v):
        self.nvs.set_i32(k, int(v))

    def get_bytes(self, k):
        buf = bytearray(64)
        try:
            n = self.nvs.get_blob(k, buf)
        except OSError:
            return None
        return bytes(buf[:n])

    def put_bytes(self, k, b):
        self.nvs.set_blob(k, b)

    def commit(self):
        self.nvs.commit()


PREFS = Prefs("park")


# ================================== LỊCH SỬ & BỘ ĐẾM ==================================
def file_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return -1


def append_history(kind, gate, uid, typ, detail, fee=0):
    detail = detail.replace(",", ";")
    try:
        new = file_size(HISTORY) <= 0
        with open(HISTORY, "a") as f:
            if new:
                f.write("time,kind,gate,uid,type,detail,fee\n")
            f.write("%s,%s,%s,%s,%s,%s,%d\n" % (now_str(), kind, gate, uid, typ, detail, fee))
        sz = file_size(HISTORY)
    except OSError:
        return
    if sz > 120000:                             # xoay vòng file khi quá ~120KB
        try:
            os.remove(HISTORY_OLD)
        except OSError:
            pass
        os.rename(HISTORY, HISTORY_OLD)


def tail_log(max_bytes=3500):
    sz = file_size(HISTORY)
    if sz < 0:
        return ""
    try:
        with open(HISTORY, "rb") as f:
            if sz > max_bytes:
                f.seek(sz - max_bytes)
                f.readline()                    # bỏ dòng bị cắt dở
            return f.read().decode()
    except (OSError, ValueError):
        return ""


def save_counters():
    PREFS.put_int("day", cnt_day)
    PREFS.put_int("in", cnt_in)
    PREFS.put_int("out", cnt_out)
    PREFS.put_int("rev", cnt_rev)
    PREFS.commit()


def check_new_day():
    global cnt_day, cnt_in, cnt_out, cnt_rev, need_count
    if not time_valid():
        return
    yday = time.gmtime(time.time() + GMT_OFFSET)[7]
    if cnt_day != yday:
        cnt_day = yday
        cnt_in = cnt_out = 0
        cnt_rev = 0
        save_counters()
        need_count = True


def count_line():
    return "ESP|COUNT|%d|%d|%d" % (cnt_in, cnt_out, cnt_rev)


# ================================== DRIVER PHẦN CỨNG ==================================
class Servo:                                    # thay cho ESP32Servo (50Hz, xung 500-2400us)
    def __init__(self, pin, min_us=500, max_us=2400):
        self.pwm = PWM(Pin(pin), freq=50)
        self.min_us = min_us
        self.max_us = max_us

    def write(self, angle):
        angle = 0 if angle < 0 else (180 if angle > 180 else angle)
        us = self.min_us + (self.max_us - self.min_us) * angle // 180
        try:
            self.pwm.duty_u16(us * 65535 // 20000)
        except AttributeError:
            self.pwm.duty(us * 1023 // 20000)


class RC522:                                    # driver MFRC522 tối giản, hai đầu đọc dùng chung SPI
    OK, NOTAG, ERR = 0, 1, 2

    def __init__(self, spi_bus, cs, rst):
        self.spi = spi_bus
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.rst = Pin(rst, Pin.OUT, value=0)
        time.sleep_ms(50)
        self.rst.value(1)
        time.sleep_ms(50)
        self.init()

    def _w(self, reg, val):
        self.cs.value(0)
        self.spi.write(bytes(((reg << 1) & 0x7E, val)))
        self.cs.value(1)

    def _r(self, reg):
        self.cs.value(0)
        self.spi.write(bytes((((reg << 1) & 0x7E) | 0x80,)))
        v = self.spi.read(1)[0]
        self.cs.value(1)
        return v

    def _set(self, reg, mask):
        self._w(reg, self._r(reg) | mask)

    def _clr(self, reg, mask):
        self._w(reg, self._r(reg) & (~mask & 0xFF))

    def init(self):
        self._w(0x01, 0x0F)                     # soft reset
        time.sleep_ms(50)
        self._w(0x2A, 0x8D)
        self._w(0x2B, 0x3E)
        self._w(0x2D, 30)
        self._w(0x2C, 0)
        self._w(0x15, 0x40)
        self._w(0x11, 0x3D)
        if not (self._r(0x14) & 0x03):          # bật anten
            self._set(0x14, 0x03)

    def version(self):
        return self._r(0x37)

    def _tocard(self, cmd, data):
        irq_en, wait_irq = 0x77, 0x30
        self._w(0x02, irq_en | 0x80)
        self._clr(0x04, 0x80)
        self._set(0x0A, 0x80)
        self._w(0x01, 0x00)
        for c in data:
            self._w(0x09, c)
        self._w(0x01, cmd)
        if cmd == 0x0C:
            self._set(0x0D, 0x80)
        i = 2000
        while True:
            n = self._r(0x04)
            i -= 1
            if not (i != 0 and not (n & 0x01) and not (n & wait_irq)):
                break
        self._clr(0x0D, 0x80)
        stat, recv, bits = self.ERR, [], 0
        if i and (self._r(0x06) & 0x1B) == 0:
            stat = self.OK
            if n & irq_en & 0x01:
                stat = self.NOTAG
            if cmd == 0x0C:
                n = self._r(0x0A)
                lbits = self._r(0x0C) & 0x07
                bits = (n - 1) * 8 + lbits if lbits else n * 8
                n = 1 if n == 0 else (16 if n > 16 else n)
                recv = [self._r(0x09) for _ in range(n)]
        return stat, recv, bits

    def _crc(self, data):
        self._clr(0x05, 0x04)
        self._set(0x0A, 0x80)
        for c in data:
            self._w(0x09, c)
        self._w(0x01, 0x03)
        i = 0xFF
        while True:
            n = self._r(0x05)
            i -= 1
            if not (i != 0 and not (n & 0x04)):
                break
        return [self._r(0x22), self._r(0x21)]

    def is_new_card(self):                      # PICC_IsNewCardPresent (REQA)
        self._w(0x0D, 0x07)
        stat, _, bits = self._tocard(0x0C, [0x26])
        return stat == self.OK and bits == 0x10

    def read_serial(self):                      # PICC_ReadCardSerial (anti-collision, UID 4 byte)
        self._w(0x0D, 0x00)
        stat, recv, _ = self._tocard(0x0C, [0x93, 0x20])
        if stat != self.OK or len(recv) != 5:
            return None
        if recv[0] ^ recv[1] ^ recv[2] ^ recv[3] != recv[4]:
            return None
        return recv[:4]

    def halt(self):                             # PICC_HaltA + PCD_StopCrypto1
        buf = [0x50, 0x00]
        buf += self._crc(buf)
        self._tocard(0x0C, buf)
        self._clr(0x08, 0x08)


def get_rfid(r):
    try:
        if not r.is_new_card():
            return 0
        raw = r.read_serial()
        if raw is None:
            return 0
        u = 0
        for b in raw:
            u = (u << 8) | b
        r.halt()
        return u
    except OSError:
        return 0


class LCD1602:                                  # LCD HD44780 qua PCF8574 (địa chỉ 0x27)
    def __init__(self, bus, addr=LCD_ADDR):
        self.i2c = bus
        self.addr = addr
        self.bl = 0x08
        time.sleep_ms(50)
        for d in (0x30, 0x30, 0x30, 0x20):
            self._nib(d, 0)
            time.sleep_ms(5)
        self._send(0x28, 0)                     # 4-bit, 2 dòng
        self._send(0x0C, 0)                     # bật hiển thị, tắt con trỏ
        self._send(0x06, 0)
        self.clear()

    def _nib(self, d, rs):
        d = (d & 0xF0) | rs | self.bl
        self.i2c.writeto(self.addr, bytes((d | 0x04, d)))

    def _send(self, b, rs):
        self._nib(b & 0xF0, rs)
        self._nib((b << 4) & 0xF0, rs)

    def clear(self):
        self._send(0x01, 0)
        time.sleep_ms(2)

    def move_to(self, col, row):
        self._send(0x80 | (col + (0x40 if row else 0)), 0)

    def putstr(self, s):
        for ch in s:
            o = ord(ch)
            self._send(o if o < 128 else 63, 1)


def lcd_show(a, b):
    # LCD (nếu bật USE_LCD) được ESP32 điều khiển trực tiếp qua I2C.
    if not USE_LCD or lcd is None:
        return
    try:
        lcd.clear()
        lcd.move_to(0, 0)
        lcd.putstr(a[:16])
        lcd.move_to(0, 1)
        lcd.putstr(b[:16])
    except OSError:
        pass


# ================================== PHẦN CỨNG BÃI XE ==================================
def now_unix():
    return local_epoch() if time_valid() else clock_t0 + millis() // 1000


def uid_str(u):
    return "%08X" % u


def send_line(s):
    publish_event(s)


def send_gate(out, opn):
    # Trạng thái hiển thị phải phản ánh cả chế độ giữ mở thủ công.
    opn = opn or (manual_hold_out if out else manual_hold_in)
    send_line("GATE|%s|%s" % ("OUT" if out else "IN", "1" if opn else "0"))


def send_deny(out, uid, why):
    send_line("DENY|%s|%s|%s" % ("OUT" if out else "IN", uid_str(uid), why))


def send_time():
    send_line("TIME|%d" % now_unix())


def is_vip_card(u):
    if u == MASTER_UID:
        return True
    for i in range(MAX_VIP_SIZE):
        if u and vip_list[i] == u:
            return True
    return False


def send_stat():
    parts = []
    for i in range(MAX_CARS):
        u = parked_uids[i]
        parts.append("%s:%d:%d" % (uid_str(u), parked_ts[i], 1 if (u and is_vip_card(u)) else 0))
    send_line("STAT|%d|%d|%d|%d|%s" % (parked_count, vip_parked_count, vip_count, MAX_CARS, ",".join(parts)))


# ----- ĐIỀU KHIỂN TRỰC TIẾP 2 SERVO BARRIER BẰNG ESP32 -----
def send_gate_state():
    # Xuất vị trí servo trực tiếp trên ESP32.
    if servo_in is None:                        # lệnh tới khi phần cứng chưa khởi tạo xong
        return
    servo_in.write(OPEN_ANGLE if gate_in_open else CLOSE_ANGLE)
    servo_out.write(OPEN_ANGLE if gate_out_open else CLOSE_ANGLE)


def gate_cmd(out, opn):
    global gate_in_open, gate_out_open
    # Tự động không được đóng cổng đang được giữ mở thủ công.
    opn = opn or (manual_hold_out if out else manual_hold_in)
    if out:
        gate_out_open = opn
    else:
        gate_in_open = opn
    send_gate_state()


def set_manual_gate(out, opn):
    global manual_hold_in, manual_hold_out
    # Báo gas vẫn cho điều khiển tay; báo cháy ưu tiên mở hai cổng.
    if fire or fire_on or admin or FLAME.value() == 0:
        return False
    if out:
        manual_hold_out = opn
    else:
        manual_hold_in = opn
    gate_cmd(out, opn)
    send_gate(out, opn)
    send_line("HOLD|%s|%s" % ("OUT" if out else "IN", "1" if opn else "0"))
    send_line("MAN|%s|%s" % ("OUT" if out else "IN", "1" if opn else "0"))
    return True


async def wait_ms(ms):                          # chờ nhưng web / WebSocket / USB vẫn chạy song song
    await asyncio.sleep_ms(ms)


def flush_telegram():
    global pending_tg
    if pending_tg:
        m = pending_tg
        pending_tg = ""
        send_telegram(m)


def set_clock(local_ep):                        # đặt giờ hệ thống từ HMI (epoch giờ địa phương)
    t = time.gmtime(local_ep - GMT_OFFSET - EPOCH_DELTA)
    machine.RTC().datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))


async def show_message(a, b, ms):
    lcd_show(a, b)
    if ms > 0:
        await wait_ms(ms)


async def beep(n):
    for _ in range(n):
        BUZZER.value(BUZ_ON)
        await wait_ms(100)
        BUZZER.value(BUZ_OFF)
        await wait_ms(100)


async def signal_error():
    for _ in range(3):
        LED_RED.value(1)
        await beep(1)
        LED_RED.value(0)
        await wait_ms(100)


async def control_barrier(ok):                  # cổng VÀO
    global busy
    if not ok:
        await signal_error()
        return
    busy = True
    LED_GREEN.value(1)
    LED_RED.value(0)
    gate_cmd(False, True)
    send_gate(False, True)
    await wait_ms(BARRIER_DELAY)
    gate_cmd(False, False)
    await wait_ms(500)
    send_gate(False, False)
    LED_GREEN.value(0)
    busy = False


async def exit_gate_signal():                   # cổng RA
    global busy
    busy = True
    LED_GREEN.value(1)
    LED_RED.value(0)
    gate_cmd(True, True)
    send_gate(True, True)
    await beep(2)
    await wait_ms(BARRIER_DELAY)
    gate_cmd(True, False)
    await wait_ms(500)
    send_gate(True, False)
    LED_GREEN.value(0)
    busy = False


def update_idle_screen():
    lcd_show("Slot Thuong:%d/%d" % (parked_count - vip_parked_count, MAX_CARS - vip_count),
             "Slot VIP   :%d/%d" % (vip_parked_count, vip_count))
    send_stat()


# ----- Lưu dữ liệu bãi xe vào flash (NVS) -----
def clear_data():
    global parked_count, vip_parked_count, vip_count, parked_uids, parked_ts, vip_list
    parked_count = vip_parked_count = vip_count = 0
    parked_uids = [0] * MAX_CARS
    parked_ts = [0] * MAX_CARS
    vip_list = [0] * MAX_VIP_SIZE


def save_data():
    PREFS.put_int("pc", parked_count)
    PREFS.put_int("vp", vip_parked_count)
    PREFS.put_int("vc", vip_count)
    PREFS.put_bytes("uid", struct.pack("<%dI" % MAX_CARS, *parked_uids))
    PREFS.put_bytes("ts", struct.pack("<%dI" % MAX_CARS, *parked_ts))
    PREFS.put_bytes("vip", struct.pack("<%dI" % MAX_VIP_SIZE, *vip_list))
    PREFS.commit()


def load_data():
    global parked_count, vip_parked_count, vip_count, parked_uids, parked_ts, vip_list
    clear_data()
    a, b, c = PREFS.get_bytes("uid"), PREFS.get_bytes("ts"), PREFS.get_bytes("vip")
    if (a is not None and b is not None and c is not None and len(a) == 4 * MAX_CARS
            and len(b) == 4 * MAX_CARS and len(c) == 4 * MAX_VIP_SIZE):
        parked_uids = list(struct.unpack("<%dI" % MAX_CARS, a))
        parked_ts = list(struct.unpack("<%dI" % MAX_CARS, b))
        vip_list = list(struct.unpack("<%dI" % MAX_VIP_SIZE, c))
        parked_count = PREFS.get_int("pc", 0)
        vip_parked_count = PREFS.get_int("vp", 0)
        vip_count = PREFS.get_int("vc", 0)
        if vip_count < 0 or vip_count > MAX_VIP_SIZE:
            vip_count = 0
            vip_list = [0] * MAX_VIP_SIZE


def reset_all_data():                           # Xóa danh sách xe và VIP từ web
    clear_data()
    save_data()
    send_line("ADMIN|RESET")
    update_idle_screen()


# ----- quản trị VIP -----
def is_car_parked(u):
    for i in range(MAX_CARS):
        if parked_uids[i] and parked_uids[i] == u:
            return True
    return False


async def try_add_vip(u):
    global vip_count
    if is_car_parked(u):
        await show_message("LOI: XE DANG GUI", "RA KHOI BAI TRUOC", 0)
        await beep(3)
        return
    if vip_count >= MAX_VIP_SIZE:
        await show_message("LOI: BO NHO FULL", "GIOI HAN VIP!", 0)
        await beep(3)
        return
    if (parked_count - vip_parked_count) + (vip_count + 1) > MAX_CARS:
        await show_message("LOI: KHONG THEM", "XE THUONG CHIEM!", 0)
        await beep(3)
        return
    vip_list[vip_count] = u
    vip_count += 1
    save_data()
    send_line("ADMIN|ADD|" + uid_str(u))
    LED_GREEN.value(1)
    await beep(1)
    await show_message("DA THEM VIP", "THANH CONG", 1000)
    LED_GREEN.value(0)


async def try_remove_vip(idx, u):
    global vip_count
    if is_car_parked(u):
        await show_message("LOI: XE DANG GUI", "RA KHOI BAI TRUOC", 2000)
        await beep(3)
        return
    for j in range(idx, MAX_VIP_SIZE - 1):
        vip_list[j] = vip_list[j + 1]
    vip_list[MAX_VIP_SIZE - 1] = 0
    vip_count -= 1
    save_data()
    send_line("ADMIN|DEL|" + uid_str(u))
    LED_RED.value(1)
    await beep(1)
    await show_message("DA XOA VIP", "THANH CONG", 1000)
    LED_RED.value(0)


async def handle_master_mode():                 # quẹt thẻ master ở cổng vào: thêm/xoá VIP, quẹt lại để thoát
    send_line("ADMIN|1")
    await show_message("ADMIN MODE", "QUET THE: VIP", 0)
    await beep(3)
    while True:
        u = get_rfid(rfid_in)
        if not u:
            await asyncio.sleep_ms(5)
            continue
        if u == MASTER_UID:
            await show_message("ADMIN MODE", "KET THUC!", 0)
            LED_RED.value(1)
            await beep(3)
            LED_RED.value(0)
            send_line("ADMIN|0")
            return
        f = -1
        for i in range(MAX_VIP_SIZE):
            if vip_list[i] == u:
                f = i
                break
        if f != -1:
            await try_remove_vip(f, u)
        else:
            await try_add_vip(u)
        await show_message("ADMIN MODE", "QUET THE: VIP", 0)
        await asyncio.sleep_ms(1)


# ----- an toàn cháy nổ -----
async def check_fire_safety():
    global fire_on, gas_warn
    if FLAME.value() == 0:
        send_line("ALARM|FIRE|1")
        fire_on = True
        gate_cmd(False, True)
        gate_cmd(True, True)
        send_gate(False, True)
        send_gate(True, True)
        lcd_show("!!! CHAY !!!", "DANG MO CONG...")
        flush_telegram()
        while FLAME.value() == 0:
            BUZZER.value(BUZ_ON)
            LED_RED.value(1)
            await wait_ms(50)
            BUZZER.value(BUZ_OFF)
            LED_RED.value(0)
            await wait_ms(50)
        await wait_ms(1000)
        fire_on = False
        gate_cmd(False, False)
        gate_cmd(True, False)
        await wait_ms(500)
        send_gate(False, False)
        send_gate(True, False)
        send_line("ALARM|FIRE|0")
        if gas_warn:
            send_line("ALARM|GAS|0")
        gas_warn = False
        update_idle_screen()
    elif MQ2.value() == 0:
        if not gas_warn:
            gas_warn = True
            send_gate_state()
            send_line("ALARM|GAS|1")
            lcd_show("! CANH BAO GAS !", "NGUY HIEM !!!")
            flush_telegram()
        BUZZER.value(BUZ_ON)
        LED_RED.value(1)
        await wait_ms(50)
        BUZZER.value(BUZ_OFF)
        LED_RED.value(0)
        await wait_ms(50)
    elif gas_warn:
        gas_warn = False
        send_gate_state()
        send_line("ALARM|GAS|0")
        update_idle_screen()


# ----- quy trình quẹt thẻ: is_exit=False cổng VÀO, True cổng RA -----
async def handle_parking(rfid, is_exit):
    global parked_count, vip_parked_count
    await beep(1)
    if rfid == MASTER_UID:
        if is_exit:
            send_deny(True, rfid, "ADMIN")
            await show_message("QUET THE ADMIN", "O CONG VAO!", 1500)
            await signal_error()
            update_idle_screen()
            return
        await handle_master_mode()
        update_idle_screen()
        return

    cur = now_unix()
    index = -1
    for i in range(MAX_CARS):
        if parked_uids[i] == rfid:
            index = i
    is_vip = is_vip_card(rfid)

    if not is_exit:                             # ===== CỔNG VÀO =====
        if index != -1:
            send_deny(False, rfid, "DUP")
            await show_message("XE DA TRONG BAI", "KHONG THE VAO", 1500)
            await signal_error()
            update_idle_screen()
            return
        if is_vip:
            allow = parked_count < MAX_CARS
        else:
            allow = (parked_count - vip_parked_count) < (MAX_CARS - vip_count) and parked_count < MAX_CARS
        if not allow:
            send_deny(False, rfid, "FULL")
            await show_message("!!! HET CHO", "VUI LONG CHO", 0)
            await control_barrier(False)
            update_idle_screen()
            return
        for i in range(MAX_CARS):
            if parked_uids[i] == 0:
                parked_uids[i] = rfid
                parked_ts[i] = cur
                parked_count += 1
                if is_vip:
                    vip_parked_count += 1
                save_data()
                tm = time.gmtime(cur - EPOCH_DELTA)
                tb = "%02d:%02d %02d/%02d" % (tm[3], tm[4], tm[2], tm[1])
                lcd_show("VIP (VAO)" if is_vip else "XE THUONG (VAO)", tb)
                send_line("EVT|IN|%s|%s|%d|%d" % (uid_str(rfid), "V" if is_vip else "N", i, cur))
                await control_barrier(True)
                update_idle_screen()
                return
    else:                                       # ===== CỔNG RA =====
        if index == -1:
            send_deny(True, rfid, "NOTIN")
            await show_message("XE CHUA VAO BAI", "KHONG THE RA", 1500)
            await signal_error()
            update_idle_screen()
            return
        dur = cur - parked_ts[index] if cur > parked_ts[index] else 0
        fee = 0 if is_vip else dur * FEE_PER_UNIT
        parked_uids[index] = 0
        parked_ts[index] = 0
        parked_count -= 1
        if is_vip:
            vip_parked_count -= 1
        save_data()
        if is_vip:
            lcd_show("VIP: MIEN PHI", "TG: %.1fp" % (dur / 60.0))
        else:
            lcd_show("TG : %.1f phut" % (dur / 60.0), "PHI: %d VND" % fee)
        send_line("EVT|OUT|%s|%s|%d|%d|%d" % (uid_str(rfid), "V" if is_vip else "N", index, dur, fee))
        await exit_gate_signal()
        update_idle_screen()


async def core_init():
    global clock_t0, spi, rfid_in, rfid_out, servo_in, servo_out, lcd
    clock_t0 = PREFS.get_int("t0", 1700000000)

    spi = SPI(2, baudrate=1000000, polarity=0, phase=0,
              sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI), miso=Pin(PIN_MISO))
    rfid_in = RC522(spi, PIN_SS_IN, PIN_RST_IN)
    rfid_out = RC522(spi, PIN_SS_OUT, PIN_RST_OUT)
    # 0x92 hoặc 0x91 = RC522 hoạt động; 0 hoặc ff = chưa nhận được (kiểm tra dây SPI/RST/nguồn 3V3)
    send_line("ESP|RFID|%x|%x" % (rfid_in.version(), rfid_out.version()))

    # Khởi tạo hai servo trực tiếp trên ESP32.
    servo_in = Servo(PIN_SERVO_IN)
    servo_out = Servo(PIN_SERVO_OUT)
    send_gate_state()                           # đưa cả hai barrier về vị trí đóng
    await asyncio.sleep_ms(500)

    if USE_LCD:
        try:
            lcd = LCD1602(i2c, LCD_ADDR)
        except OSError:
            lcd = None
    await show_message("HE THONG GIU XE", "NHOM 18", 1000)
    load_data()
    update_idle_screen()
    send_line("SYS|READY")


async def core_loop():
    global t_time, t_stat, t_save
    await check_fire_safety()
    if since(t_time) >= 1000:
        t_time = millis()
        send_time()
    if since(t_stat) >= 3000:
        t_stat = millis()
        send_stat()
    if since(t_save) >= 60000:
        t_save = millis()
        if time_valid():
            PREFS.put_int("t0", local_epoch())
            PREFS.commit()
        gc.collect()
    if not gas_warn:
        u = get_rfid(rfid_in)
        if u:
            await handle_parking(u, False)
        u = get_rfid(rfid_out)
        if u:
            await handle_parking(u, True)


# src: -1 = USB, >=0 = số client WebSocket, -2 = web HTTP
def handle_command(c, src):
    c = c.strip()
    if not c:
        return
    k = c[0]
    if src >= 0 and k != "S":
        return                                  # WebSocket chỉ được xin trạng thái, không được điều khiển
    if k == "S":
        replay(src)
        send_time()
        send_stat()
    elif c in ("I", "O", "i", "o"):
        out = c in ("O", "o")
        opn = c in ("I", "O")
        if not set_manual_gate(out, opn):
            send_line("ESP|CMD|BUSY")
    elif k == "T":
        t = to_int(c[1:])
        if t > 1600000000:
            set_clock(t)
            send_line("OK")
            send_time()


# ================================== USB SERIAL ==================================
try:
    _poll = select.poll()
    _poll.register(sys.stdin, select.POLLIN)
except Exception:
    _poll = None


def _usb_read():                                # trả về 1 ký tự nếu có, ngược lại None
    if _poll is None or not _poll.poll(0):
        return None
    b = sys.stdin.buffer.read(1)
    return chr(b[0]) if b else None


def service_usb():
    global usb_buf
    while True:
        ch = _usb_read()
        if ch is None:
            break
        if ch == "\n":
            command = usb_buf
            usb_buf = ""
            handle_command(command, -1)
        elif ch != "\r" and len(usb_buf) < 40:
            usb_buf += ch


async def usb_task():
    while True:
        try:
            service_usb()
        except Exception as e:
            log_exc(e)
        await asyncio.sleep_ms(5)


# ================================== PHÂN TÍCH GÓI TIN NỘI BỘ ==================================
def parse_stat(p):
    global st_parked, st_vip, st_max
    st_parked = to_int(p[1])
    st_vip = to_int(p[2])
    st_max = to_int(p[4])
    tk = p[5].split(",")[:4]
    for i in range(4):
        slots[i] = ["", 0, False]
        if i < len(tk):
            f = (tk[i].split(":") + ["", "", ""])[:3]
            if f[0] != "00000000" and len(f[0]) == 8:
                slots[i] = [f[0], to_int(f[1]), f[2] == "1"]


def parse_line(l):
    global rtc_unix, rtc_at, last_stat, gate_in, gate_out, fire, gas_a, admin
    global pending_tg, last_event, cnt_in, cnt_out, cnt_rev, need_count
    p = l.split("|")[:8]
    n = len(p)
    p += [""] * (8 - n)
    k = p[0]

    if k == "TIME" and n >= 2:
        rtc_unix = to_int(p[1])
        rtc_at = millis()
    elif k == "STAT" and n >= 6:
        parse_stat(p)
        last_stat = l
    elif k == "GATE" and n >= 3:
        o = (p[2] == "1")
        if p[1] == "IN":
            gate_in = o
        else:
            gate_out = o
    elif k == "ALARM" and n >= 3:
        on = (p[2] == "1")
        if p[1] == "FIRE":
            if on and not fire:
                pending_tg = "🔥 PHAT HIEN CHAY tai bai do xe! Da mo 2 cong thoat hiem."
            fire = on
            last_event = "!!! CHAY !!!" if on else "Het chay"
            append_history("ALARM", "", "", "", "Phát hiện cháy" if on else "Hết cháy")
        else:
            if on and not gas_a:
                pending_tg = "⚠️ CANH BAO RO RI GAS tai bai do xe!"
            gas_a = on
            last_event = "CANH BAO GAS" if on else "Het gas"
            append_history("ALARM", "", "", "", "Cảnh báo khí gas" if on else "Hết cảnh báo gas")
    elif k == "EVT" and n >= 6 and p[1] == "IN":
        check_new_day()
        cnt_in += 1
        save_counters()
        need_count = True
        t = "VIP" if p[3] == "V" else "Thường"
        slot = str(to_int(p[4]) + 1)
        last_event = "VAO " + p[2][4:] + " P" + slot
        append_history("IN", "VÀO", p[2], t, "Cho vào - ô P" + slot)
    elif k == "EVT" and n >= 7 and p[1] == "OUT":
        check_new_day()
        cnt_out += 1
        cnt_rev += to_int(p[6])
        save_counters()
        need_count = True
        t = "VIP" if p[3] == "V" else "Thường"
        last_event = "RA " + p[2][4:] + " " + p[6] + "d"
        append_history("OUT", "RA", p[2], t,
                       "Ô P" + str(to_int(p[4]) + 1) + " | " + p[5] + "s | " + p[6] + " VND", to_int(p[6]))
    elif k == "DENY" and n >= 4:
        last_event = "TU CHOI " + p[3]
        append_history("DENY", "VÀO" if p[1] == "IN" else "RA", p[2], "", "Từ chối: " + p[3])
    elif k == "ADMIN" and n >= 2:
        if p[1] == "1":
            admin = True
            last_event = "ADMIN MODE"
        elif p[1] == "0":
            admin = False
            last_event = "Thoat admin"
        elif p[1] == "ADD" or p[1] == "DEL":
            append_history("SYS", "VÀO", p[2], "VIP", "Thêm thẻ VIP" if p[1] == "ADD" else "Xoá thẻ VIP")
        elif p[1] == "RESET":
            append_history("SYS", "VÀO", "", "", "Admin reset toàn bộ dữ liệu")
    elif k == "MAN" and n >= 2:
        opn = (n < 3 or p[2] != "0")            # tương thích sự kiện MAN cũ
        last_event = ("MO THU CONG " if opn else "DONG THU CONG ") + p[1]
        append_history("MAN", "VÀO" if p[1] == "IN" else "RA", "", "",
                       "Mở barrier thủ công" if opn else "Đóng barrier thủ công")


def publish_event(l):
    global need_count
    l = l.strip()
    if not l:
        return
    parse_line(l)
    emit(l)
    if need_count:
        need_count = False
        emit(count_line())


# ================================== PHÁT LẠI TRẠNG THÁI CHO CLIENT MỚI ==================================
def send_to(dst, s):
    if not s:
        return
    if dst == -1:
        print(s)
    elif dst >= 0:
        c = ws_clients.get(dst)
        if c:
            c.push(s)


def current_ip():
    return sta.ifconfig()[0] if sta.isconnected() else ap.ifconfig()[0]


def replay(dst):
    if dst < -1:
        return
    if last_stat:
        send_to(dst, last_stat)
    if rtc_unix:
        send_to(dst, "TIME|%d" % (rtc_unix + since(rtc_at) // 1000))
    send_to(dst, "GATE|IN|" + ("1" if gate_in else "0"))
    send_to(dst, "GATE|OUT|" + ("1" if gate_out else "0"))
    if fire:
        send_to(dst, "ALARM|FIRE|1")
    if gas_a:
        send_to(dst, "ALARM|GAS|1")
    if admin:
        send_to(dst, "ADMIN|1")
    send_to(dst, "HOLD|IN|" + ("1" if manual_hold_in else "0"))
    send_to(dst, "HOLD|OUT|" + ("1" if manual_hold_out else "0"))
    send_to(dst, count_line())
    try:
        rssi = sta.status("rssi") if sta.isconnected() else 0
    except Exception:
        rssi = 0
    send_to(dst, "ESP|NET|%s|%s" % (current_ip(), rssi))


# ================================== WEBSOCKET (cổng 81) ==================================
class WsClient:
    def __init__(self, num, writer):
        self.num = num
        self.w = writer
        self.q = []
        self.ev = asyncio.Event()
        self.alive = True

    def push_raw(self, frame):
        if len(self.q) < 40:                    # client quá chậm thì bỏ bớt, tránh tràn RAM
            self.q.append(frame)
            self.ev.set()

    def push(self, text):
        self.push_raw(ws_frame(1, text.encode()))


def ws_frame(opcode, payload):
    n = len(payload)
    if n < 126:
        hdr = bytes((0x80 | opcode, n))
    else:
        hdr = bytes((0x80 | opcode, 126)) + struct.pack(">H", n)
    return hdr + payload


def ws_broadcast(line):
    if not ws_clients:
        return
    frame = ws_frame(1, line.encode())
    for c in ws_clients.values():
        c.push_raw(frame)


async def ws_sender(c):                         # chỉ riêng tác vụ này được ghi vào socket của client
    try:
        while c.alive:
            await c.ev.wait()
            c.ev.clear()
            frames, c.q = c.q, []
            for f in frames:
                c.w.write(f)
                await c.w.drain()
    except Exception:
        c.alive = False


async def ws_read_frame(reader):
    h = await reader.readexactly(2)
    opcode = h[0] & 0x0F
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", await reader.readexactly(2))[0]
    elif ln == 127:
        raise ValueError("frame qua lon")
    mask = await reader.readexactly(4) if (h[1] & 0x80) else None
    if ln > 512:
        raise ValueError("frame qua lon")
    data = await reader.readexactly(ln) if ln else b""
    if mask:
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return opcode, data


async def ws_handler(reader, writer):
    c = None
    try:
        key = None
        for _ in range(30):
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line or line == b"\r\n":
                break
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip()
        free = [n for n in range(WS_MAX) if n not in ws_clients]
        if key is None or not free:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        accept = binascii.b2a_base64(hashlib.sha1(key + WS_GUID).digest()).strip()
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        await writer.drain()
        c = WsClient(free[0], writer)
        ws_clients[c.num] = c
        asyncio.create_task(ws_sender(c))
        replay(c.num)                           # WStype_CONNECTED
        while True:
            op, data = await ws_read_frame(reader)
            if op == 1:                         # WStype_TEXT
                handle_command(data.decode(), c.num)
            elif op == 9:                       # ping -> pong
                c.push_raw(ws_frame(10, data))
            elif op == 8:                       # close
                break
    except Exception:
        pass
    finally:
        if c is not None:
            c.alive = False
            ws_clients.pop(c.num, None)
            c.ev.set()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ================================== OLED ==================================
def oled_big_text(text, x, y, scale):           # chữ phóng to (thay cho setTextSize của Adafruit)
    w = len(text) * 8
    buf = bytearray(w)
    fb = framebuf.FrameBuffer(buf, w, 8, framebuf.MONO_VLSB)
    fb.text(text, 0, 0, 1)
    for cx in range(w):
        col = buf[cx]
        if col:
            for cy in range(8):
                if (col >> cy) & 1:
                    oled.fill_rect(x + cx * scale, y + cy * scale, scale, scale, 1)


def draw_oled():
    if not oled_ok:
        return
    oled.fill(0)
    oled.text(current_ip() if sta.isconnected() else "AP " + ap.ifconfig()[0], 0, 0, 1)
    alarm_txt = "!!! CHAY !!!" if fire else ("! CANH BAO GAS !" if gas_a else None)
    if alarm_txt:
        oled.fill_rect(0, 12, 128, 20, 1)
        oled.text(alarm_txt, 0, 18, 0)
    else:
        oled_big_text("%d/%d" % (max(0, st_max - st_parked), st_max), 0, 14, 3)
        oled.text("TRONG", 84, 14, 1)
        oled.text("V:%d" % cnt_in, 84, 24, 1)
        oled.text("R:%d" % cnt_out, 84, 33, 1)
    oled.text("IN:" + ("MO  " if gate_in else "DONG") + " OUT:" + ("MO" if gate_out else "DONG"), 0, 44, 1)
    oled.text(last_event[:16], 0, 55, 1)
    oled.show()


# ================================== TELEGRAM ==================================
def send_telegram(msg):
    if not TG_TOKEN or not sta.isconnected():
        return
    try:
        try:
            import urequests as requests
        except ImportError:
            import requests
        body = json.dumps({"chat_id": TG_CHAT, "text": msg}).encode()
        r = requests.post("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
                          data=body, headers={"Content-Type": "application/json"})
        r.close()
    except Exception as e:
        log_exc(e)


# ================================== WEB DASHBOARD (cổng 80) ==================================
INDEX_HTML = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smart Parking</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,sans-serif;background:#0d1117;color:#e6edf3}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:#161b22}
h1{font-size:18px;margin:0}#dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#f85149;margin-right:6px}
#alarm{display:none;padding:10px;text-align:center;font-weight:700;animation:bl 1s infinite}@keyframes bl{50%{opacity:.4}}
.wrap{padding:12px;max-width:900px;margin:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.card{background:#161b22;border-radius:12px;padding:10px;text-align:center}.card b{display:block;font-size:28px}.card span{font-size:11px;color:#8b949e}
.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
.slot{border:2px solid #3fb950;background:#12261a;border-radius:12px;padding:10px 4px;text-align:center;min-height:120px;font-size:12px}
.slot.occ{border-color:#f85149;background:#2a1518}.slot .car{font-size:30px}
.gates{display:flex;gap:10px;margin-bottom:12px}.gate{flex:1;padding:10px;border-radius:10px;text-align:center;background:#161b22}.gate.open{background:#14301d;color:#3fb950}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}button{flex:1;background:#1f6feb;color:#fff;border:0;border-radius:10px;padding:14px;font-size:15px}button.o{background:#8957e5}
#log{list-style:none;padding:0;margin:0;font-size:13px}#log li{padding:5px 8px;border-bottom:1px solid #21262d}
.IN{color:#3fb950}.OUT{color:#58a6ff}.DENY{color:#f85149}.ALARM{color:#f0883e}.MAN{color:#d2a8ff}.SYS{color:#8b949e}a{color:#58a6ff}
</style></head><body>
<header><h1>SMART PARKING</h1><div><span id="dot"></span><span id="net">...</span></div></header>
<div id="alarm"></div>
<div class="wrap">
<div class="cards"><div class="card"><b id="free">0</b><span>CHỖ TRỐNG</span></div><div class="card"><b id="parked">0</b><span>ĐANG ĐỖ</span></div>
<div class="card"><b id="in">0</b><span>LƯỢT VÀO</span></div><div class="card"><b id="out">0</b><span>LƯỢT RA</span></div>
<div class="card"><b id="rev">0</b><span>DOANH THU</span></div></div>
<div class="slots" id="slots"></div>
<div class="gates"><div class="gate" id="gIn">CỔNG VÀO: ĐÓNG</div><div class="gate" id="gOut">CỔNG RA: ĐÓNG</div></div>
<div class="btns"><button onclick="openGate('in')">MỞ CỔNG VÀO</button><button class="o" onclick="closeGate('in')">ĐÓNG CỔNG VÀO</button><button onclick="openGate('out')">MỞ CỔNG RA</button><button class="o" onclick="closeGate('out')">ĐÓNG CỔNG RA</button></div>
<div class="btns"><button style="background:#da3633" onclick="resetAll()">XÓA DỮ LIỆU</button></div>
<h3>Lịch sử <a href="/history.csv" style="font-size:13px">(tải CSV)</a></h3><ul id="log"></ul></div>
<script>
const $=id=>document.getElementById(id);
let S={parked:0,max:4,slots:[],rtc:0,at:0},C={i:0,o:0,r:0},fire=0,gas=0;
function dot(o){$('dot').style.background=o?'#3fb950':'#f85149';$('net').textContent=o?'Đã kết nối':'Mất kết nối'}
function fmt(s){s=Math.max(0,s|0);const h=s/3600|0,m=s%3600/60|0;return[h,m,s%60].map(x=>String(x).padStart(2,'0')).join(':')}
function now(){return S.rtc?S.rtc+(Date.now()-S.at)/1000:0}
function render(){
$('free').textContent=Math.max(0,S.max-S.parked);$('parked').textContent=S.parked+'/'+S.max;
$('in').textContent=C.i;$('out').textContent=C.o;$('rev').textContent=C.r.toLocaleString();
let h='';for(let i=0;i<S.max;i++){const s=S.slots[i];
if(s){const el=now()?now()-s.ts:0;h+=`<div class="slot occ"><div>P${i+1}</div><div class="car">🚗</div><div>${s.uid.slice(-4)}${s.vip?' ⭐':''}</div><div>${fmt(el)}</div><div>${s.vip?'Miễn phí':Math.round(el)+' đ'}</div></div>`}
else h+=`<div class="slot"><div>P${i+1}</div><div class="car">🅿️</div><div>TRỐNG</div></div>`}
$('slots').innerHTML=h;
const a=$('alarm');
if(fire){a.style.display='block';a.style.background='#b62324';a.textContent='PHÁT HIỆN CHÁY - ĐÃ MỞ 2 CỔNG THOÁT HIỂM'}
else if(gas){a.style.display='block';a.style.background='#9e6a03';a.textContent='CẢNH BÁO RÒ RỈ KHÍ GAS - KHOÁ QUẸT THẺ'}
else a.style.display='none'}
function add(k,t,when){const li=document.createElement('li');li.className=k;li.textContent=(when||new Date().toLocaleTimeString())+'  '+t;$('log').prepend(li);while($('log').children.length>60)$('log').lastChild.remove()}
function handle(l){const p=l.trim().split('|');switch(p[0]){
case'TIME':S.rtc=+p[1];S.at=Date.now();break;
case'STAT':S.parked=+p[1];S.max=+p[4];S.slots=p[5].split(',').map(t=>{const[u,ts,v]=t.split(':');return u==='00000000'?null:{uid:u,ts:+ts,vip:v==='1'}});render();break;
case'GATE':{const e=$(p[1]==='IN'?'gIn':'gOut'),o=p[2]==='1';e.className='gate'+(o?' open':'');e.textContent=(p[1]==='IN'?'CỔNG VÀO: ':'CỔNG RA: ')+(o?'MỞ':'ĐÓNG');break}
case'EVT':add(p[1],(p[1]==='IN'?'VÀO ':'RA ')+p[2]+' ô P'+(+p[4]+1)+(p[1]==='OUT'?' - '+fmt(+p[5])+' - '+p[6]+' đ':''));break;
case'DENY':add('DENY','TỪ CHỐI '+(p[1]==='IN'?'VÀO ':'RA ')+p[2]+' ('+p[3]+')');break;
case'ALARM':if(p[1]==='FIRE')fire=+p[2];else gas=+p[2];add('ALARM',p[1]+(p[2]==='1'?' BẬT':' TẮT'));render();break;
case'MAN':add('MAN',(p[2]==='0'?'Đóng':'Mở')+' thủ công cổng '+(p[1]==='IN'?'VÀO':'RA'));break;
case'ESP':if(p[1]==='COUNT'){C={i:+p[2],o:+p[3],r:+p[4]};render()}}}
function conn(){const w=new WebSocket('ws://'+location.hostname+':81');w.onopen=()=>dot(1);
w.onclose=()=>{dot(0);setTimeout(conn,2000)};w.onmessage=e=>e.data.split('\n').forEach(handle)}
function openGate(g){const pin=prompt('Nhập PIN để mở cổng');if(pin===null)return;
fetch('/api/open?gate='+g+'&pin='+encodeURIComponent(pin)).then(r=>r.text()).then(t=>alert(t))}
function closeGate(g){const pin=prompt('Nhập PIN để đóng cổng');if(pin===null)return;
fetch('/api/close?gate='+g+'&pin='+encodeURIComponent(pin)).then(r=>r.text()).then(t=>alert(t))}
function resetAll(){if(!confirm('Xóa toàn bộ xe đang đỗ và danh sách VIP?'))return;const pin=prompt('Nhập PIN');if(pin===null)return;fetch('/api/reset?pin='+encodeURIComponent(pin)).then(r=>r.text()).then(t=>alert(t))}
fetch('/api/log').then(r=>r.text()).then(t=>{t.trim().split('\n').slice(-30).forEach(r=>{const c=r.split(',');
if(c.length>=6&&c[0]!=='time')add(c[1],c[2]+' '+c[3]+' '+c[5],c[0].slice(5))})});
render();setInterval(render,1000);conn();
</script></body></html>"""

REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}


async def http_send(w, code, ctype, body, extra=""):
    if isinstance(body, str):
        body = body.encode()
    head = "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n%sConnection: close\r\n\r\n" % (
        code, REASONS.get(code, "OK"), ctype, len(body), extra)
    w.write(head.encode())
    w.write(body)
    await w.drain()


def url_decode(s):
    b = s.replace("+", " ").encode()
    out = bytearray()
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if c == 0x25 and i + 2 < n:
            try:
                out.append(int(b[i + 1:i + 3].decode(), 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return out.decode()


def parse_qs(qs):
    args = {}
    for part in qs.split("&"):
        if part:
            k, _, v = part.partition("=")
            args[url_decode(k)] = url_decode(v)
    return args


TXT = "text/plain; charset=utf-8"


async def route(w, path, args):
    if path == "/":
        await http_send(w, 200, "text/html; charset=utf-8", INDEX_HTML)
    elif path == "/api/state":
        j = json.dumps({
            "parked": st_parked, "vipParked": st_vip, "max": st_max,
            "free": max(0, st_max - st_parked), "gateIn": gate_in, "gateOut": gate_out,
            "fire": fire, "gas": gas_a, "in": cnt_in, "out": cnt_out,
            "revenue": cnt_rev, "time": now_str()})
        await http_send(w, 200, "application/json", j)
    elif path == "/api/log":
        await http_send(w, 200, TXT, tail_log())
    elif path == "/history.csv":
        try:
            f = open(HISTORY, "rb")
        except OSError:
            await http_send(w, 404, "text/plain", "Chua co lich su")
            return
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/csv; charset=utf-8\r\n"
                b"Content-Disposition: attachment; filename=lich_su_bai_do_xe.csv\r\n"
                b"Connection: close\r\n\r\n")
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            w.write(chunk)
            await w.drain()
        f.close()
        await w.drain()
    elif path == "/api/open" or path == "/api/close":
        opening = (path == "/api/open")
        if args.get("pin", "") != WEB_PIN:
            await http_send(w, 403, TXT, "Sai PIN")
            return
        gate = args.get("gate", "")
        if gate != "in" and gate != "out":
            await http_send(w, 400, TXT, "Cổng không hợp lệ")
            return
        if not set_manual_gate(gate == "out", opening):
            await http_send(w, 409, TXT, "Đang báo cháy hoặc ở chế độ quản trị")
            return
        if opening:
            msg = "Đã giữ mở cổng VÀO đến khi bấm ĐÓNG" if gate == "in" else "Đã giữ mở cổng RA đến khi bấm ĐÓNG"
        else:
            msg = "Đã đóng cổng VÀO" if gate == "in" else "Đã đóng cổng RA"
        await http_send(w, 200, TXT, msg)
    elif path == "/api/reset":
        if args.get("pin", "") != WEB_PIN:
            await http_send(w, 403, TXT, "Sai PIN")
            return
        if busy or admin or fire:
            await http_send(w, 409, TXT, "Hệ thống đang bận")
            return
        reset_all_data()
        await http_send(w, 200, TXT, "Đã xóa dữ liệu")
    else:
        await http_send(w, 404, "text/plain", "Not Found")


async def http_handler(reader, writer):
    try:
        reqline = await asyncio.wait_for(reader.readline(), 5)
        for _ in range(40):                     # bỏ qua các header
            h = await asyncio.wait_for(reader.readline(), 5)
            if not h or h == b"\r\n":
                break
        parts = reqline.decode().split()
        if len(parts) >= 2:
            path, _, qs = parts[1].partition("?")
            await route(writer, path, parse_qs(qs))
    except Exception as e:
        log_exc(e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ================================== MẠNG: WiFi, NTP, OTA ==================================
NTP_HOSTS = ("pool.ntp.org", "time.google.com")
ntp_idx = 0
t_ntp = 0
ntp_interval = 0                                # 0 = đồng bộ ngay khi có WiFi
t_wifi = 0


def wifi_start():
    global sta, ap, t_wifi
    try:
        network.hostname(HOSTNAME)
    except Exception:
        pass
    ap = network.WLAN(network.AP_IF)            # ESP32 luôn phát thêm WiFi riêng để dự phòng
    ap.active(True)
    try:
        ap.config(essid=AP_SSID, password=AP_PASS, authmode=network.AUTH_WPA2_PSK)
    except Exception:
        ap.config(essid=AP_SSID, password=AP_PASS)
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.connect(WIFI_SSID, WIFI_PASS)
    except OSError:
        pass
    t_wifi = millis()


def sync_ntp():
    global ntp_idx, t_ntp, ntp_interval
    import ntptime
    ntptime.host = NTP_HOSTS[ntp_idx]
    try:
        ntptime.settime()                       # đặt RTC theo UTC
        ntp_interval = 3600000                  # đồng bộ lại mỗi giờ
    except Exception:
        ntp_idx = (ntp_idx + 1) % len(NTP_HOSTS)
        ntp_interval = 30000                    # thử lại sau 30 giây
    t_ntp = millis()


def start_ota():                                # thay ArduinoOTA: nạp code qua WiFi bằng WebREPL
    global ota_on
    ota_on = True
    try:
        import webrepl
        webrepl.start(password=OTA_PASS)
    except Exception as e:
        log_exc(e)


# ================================== SETUP / LOOP ==================================
async def main_loop():
    global t_wifi
    t_oled = t_day = millis()
    while True:
        try:
            # ---- dịch vụ mạng khi có WiFi ----
            if sta.isconnected():
                if not ota_on:
                    start_ota()
                if since(t_ntp) >= ntp_interval:
                    sync_ntp()
            elif since(t_wifi) > 10000 and sta.status() != network.STAT_CONNECTING:
                t_wifi = millis()               # tự kết nối lại (thay setAutoReconnect)
                try:
                    sta.connect(WIFI_SSID, WIFI_PASS)
                except OSError:
                    pass

            await core_loop()
            flush_telegram()

            if since(t_oled) > 500:
                t_oled = millis()
                try:
                    draw_oled()
                except OSError:
                    pass
            if since(t_day) > 60000:
                t_day = millis()
                check_new_day()
        except Exception as e:
            log_exc(e)
        await asyncio.sleep_ms(2)


async def main():
    global cnt_day, cnt_in, cnt_out, cnt_rev, i2c, oled, oled_ok
    # USB serial dùng luôn UART0 của MicroPython (115200) -> chỉ cần đọc/ghi stdin/stdout
    asyncio.create_task(usb_task())

    cnt_day = PREFS.get_int("day", -1)
    cnt_in = PREFS.get_int("in", 0)
    cnt_out = PREFS.get_int("out", 0)
    cnt_rev = PREFS.get_int("rev", 0)

    i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=400000)
    try:
        from ssd1306 import SSD1306_I2C
        oled = SSD1306_I2C(128, 64, i2c, OLED_ADDR)
        oled_ok = True
        oled.fill(0)
        oled.text("SMART PARKING", 0, 0, 1)
        oled.text("ESP32 controller", 0, 10, 1)
        oled.text("...", 0, 20, 1)
        oled.show()
    except Exception:
        oled_ok = False

    wifi_start()
    await asyncio.start_server(http_handler, "0.0.0.0", HTTP_PORT)
    await asyncio.start_server(ws_handler, "0.0.0.0", WS_PORT)

    await core_init()                           # khởi tạo phần cứng bãi xe, gửi SYS|READY
    append_history("SYS", "", "", "", "ESP32 controller khởi động")
    await main_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.new_event_loop()                # xoá trạng thái uasyncio để chạy lại được
