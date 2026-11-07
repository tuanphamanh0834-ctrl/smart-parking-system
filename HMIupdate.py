"""
SMART PARKING HMI - PRJ09
Cài thư viện:  pip install customtkinter pyserial
Chạy:          python hmismart.py
Nạp firmware smart_parking_esp32_direct_servo.ino cho ESP 32 trước, nối USB, chọn cổng COM rồi bấm KẾT NỐI.
Chưa có ESP 32? Bấm "CHẠY DEMO" để xem mô hình hoạt động.
"""
import os
import csv
import math
import time
import queue
import random
import shutil
import calendar
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timezone

import customtkinter as ctk
import serial
from serial.tools import list_ports

try:
    import winsound
except ImportError:
    winsound = None

BAUD = 115200
MAX_CARS = 4
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parking_history.csv")

# ---------- hình học mô hình ----------
CW, CH = 820, 560
LANE_Y = 310
GATE_IN_X, GATE_OUT_X = 120, 700
SLOT_X = [209, 343, 477, 611]
SLOT_Y1, SLOT_Y2, SLOT_CY = 72, 240, 156

CAR_COLORS = ["#e5484d", "#3b82f6", "#22c55e", "#a855f7", "#f97316", "#06b6d4", "#ec4899", "#94a3b8"]


def local_epoch():
    return calendar.timegm(time.localtime())


def fmt_dt(u):
    return datetime.fromtimestamp(u, timezone.utc).strftime("%H:%M:%S %d/%m")


def fmt_dur(s):
    m, s = divmod(int(max(0, s)), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def shade(hexcol, f=0.7):
    r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def rand_uid():
    return "%08X" % random.randrange(1 << 32)


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


# =====================================================================================
#  BARRIER
# =====================================================================================
class Barrier:
    CLOSED = -90.0

    def __init__(self, cv, px, py, open_angle):
        self.cv, self.px, self.py = cv, px, py
        self.open_a = open_angle
        self.angle = self.CLOSED
        self.target = self.CLOSED
        self.length = 80
        cv.create_rectangle(px - 13, py - 4, px + 13, py + 20, fill="#3b4656", outline="#6e7a8a", tags=("barrier",))
        self.base = cv.create_line(px, py, px, py, width=9, fill="#d93a3a", capstyle="round", tags=("barrier",))
        self.stripe = cv.create_line(px, py, px, py, width=9, fill="white", dash=(9, 9), tags=("barrier",))
        self.led = cv.create_oval(px - 5, py + 4, px + 5, py + 14, fill="#d93a3a", outline="", tags=("barrier",))
        self.draw()

    def set_open(self, o):
        self.target = self.open_a if o else self.CLOSED
        self.cv.itemconfig(self.led, fill="#3fb950" if o else "#d93a3a")

    def is_open(self):
        return abs(self.angle - self.CLOSED) > 40

    def step(self):
        d = self.target - self.angle
        if abs(d) > 0.1:
            self.angle += max(-7, min(7, d))
            self.draw()

    def draw(self):
        r = math.radians(self.angle)
        x2 = self.px + self.length * math.cos(r)
        y2 = self.py + self.length * math.sin(r)
        self.cv.coords(self.base, self.px, self.py, x2, y2)
        self.cv.coords(self.stripe, self.px, self.py, x2, y2)


# =====================================================================================
#  XE Ô TÔ MINI
# =====================================================================================
class Car:
    def __init__(self, cv, uid, vip, x, y, heading):
        self.cv, self.uid, self.vip = cv, uid, vip
        self.x, self.y, self.heading = x, y, heading
        self.path = []
        self.speed = 6.0
        self.on_done = None
        color = "#f5b301" if vip else random.choice(CAR_COLORS)
        body = [(-27, -10), (-23, -14), (23, -14), (27, -10), (27, 10), (23, 14), (-23, 14), (-27, 10)]
        parts = [
            (rect(-20, -17, -9, -13), "#0b0b0b"), (rect(9, -17, 20, -13), "#0b0b0b"),
            (rect(-20, 13, -9, 17), "#0b0b0b"), (rect(9, 13, 20, 17), "#0b0b0b"),
            (body, color),
            (rect(-14, -8, -10, 8), "#2b3a4a"),
            (rect(-10, -9, 5, 9), shade(color)),
            ([(5, -9), (12, -7), (12, 7), (5, 9)], "#9bd6ff"),
            (rect(25, -12, 28, -7), "#fff3a0"), (rect(25, 7, 28, 12), "#fff3a0"),
            (rect(-28, -12, -25, -8), "#ff3b3b"), (rect(-28, 8, -25, 12), "#ff3b3b"),
        ]
        self.local = [p for p, _ in parts]
        self.items = [cv.create_polygon(0, 0, 0, 0, 0, 0, fill=f, outline="") for _, f in parts]
        tail = uid[-4:] + (" VIP" if vip else "")
        self.label = cv.create_text(x, y - 34, text=tail, fill="#e6edf3", font=("Segoe UI", 9, "bold"))
        self.draw()

    def draw(self):
        r = math.radians(self.heading)
        c, s = math.cos(r), math.sin(r)
        for it, pts in zip(self.items, self.local):
            flat = []
            for lx, ly in pts:
                flat += [self.x + lx * c - ly * s, self.y + lx * s + ly * c]
            self.cv.coords(it, *flat)
        self.cv.coords(self.label, self.x, self.y - 34)

    def moving(self):
        return bool(self.path)

    def step(self):
        if not self.path:
            return
        p = self.path[0]
        tx, ty = p[0], p[1]
        rev = len(p) > 2 and p[2] == "r"
        dx, dy = tx - self.x, ty - self.y
        d = math.hypot(dx, dy)
        if rev:
            sp = self.speed * 0.7
        else:
            sp = self.speed
            if d > 0.5:
                want = math.degrees(math.atan2(dy, dx))
                diff = (want - self.heading + 180) % 360 - 180
                self.heading += max(-9, min(9, diff))
                sp = self.speed * max(0.3, 1 - abs(diff) / 100)
        self.heading = (self.heading + 180) % 360 - 180
        if d <= sp:
            self.x, self.y = tx, ty
            self.path.pop(0)
            self.draw()
            if not self.path and self.on_done:
                cb, self.on_done = self.on_done, None
                cb()
        else:
            self.x += dx / d * sp
            self.y += dy / d * sp
            self.draw()

    def delete(self):
        for it in self.items:
            self.cv.delete(it)
        self.cv.delete(self.label)


# =====================================================================================
#  ỨNG DỤNG
# =====================================================================================
class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title("SMART PARKING HMI - PRJ09")
        self.geometry("1560x900")
        self.minsize(1380, 820)

        self.ser = None
        self.running = False
        self.q = queue.Queue()
        self.last_rx = 0.0
        self.slots = [None] * MAX_CARS
        self.slot_cars = [None] * MAX_CARS
        self.cars = []
        self.stat = dict(parked=0, vip_parked=0, vip_cnt=0, max=MAX_CARS)
        self.counts = dict(IN=0, OUT=0, rev=0)
        self.fire = self.gas = self.admin = False
        self.rtc_unix = None
        self.rtc_mono = 0.0
        self.lcd_hold = 0.0
        self.frame = 0
        self.demo = False
        self.manual_hold = {"IN": False, "OUT": False}
        self.gate_open = {"IN": False, "OUT": False}
        self.demo_gate_jobs = {"IN": None, "OUT": None}
        self.demo_step_job = None
        self.demo_fire_job = None
        self.d_slots = [None] * MAX_CARS
        self.port_var = ctk.StringVar()

        self.build_ui()
        self.load_history()
        self.refresh_ports()
        self.update_counters()
        self.update_slots_visual()
        self.after(30, self.anim_tick)
        self.after(40, self.poll)
        self.after(1000, self.sec_tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        self.grid_columnconfigure(1, weight=0)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, height=54, corner_radius=0, fg_color="#0d1117")
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        ctk.CTkLabel(top, text="SMART PARKING  |  PRJ09 HMI", font=ctk.CTkFont("Segoe UI", 20, "bold")).pack(side="left", padx=18, pady=10)
        self.lbl_rtc = ctk.CTkLabel(top, text="ESP32: --", font=ctk.CTkFont("Consolas", 15, "bold"), text_color="#58a6ff")
        self.lbl_rtc.pack(side="right", padx=18)
        self.lbl_pc = ctk.CTkLabel(top, text="PC: --", font=ctk.CTkFont("Consolas", 15, "bold"), text_color="#8b949e")
        self.lbl_pc.pack(side="right", padx=10)

        left = ctk.CTkFrame(self, width=290, corner_radius=0)
        left.grid(row=1, column=0, sticky="ns")
        left.grid_propagate(False)
        center = ctk.CTkFrame(self, corner_radius=0, fg_color="#0d1117")
        center.grid(row=1, column=1, sticky="ns")
        right = ctk.CTkFrame(self, corner_radius=0)
        right.grid(row=1, column=2, sticky="nsew")

        self.build_left(left)
        self.build_center(center)
        self.build_right(right)

    def section(self, p, text):
        ctk.CTkLabel(p, text=text, font=ctk.CTkFont("Segoe UI", 12, "bold"), text_color="#8b949e").pack(anchor="w", padx=14, pady=(14, 4))

    def build_left(self, p):
        self.section(p, "KẾT NỐI USB")
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x", padx=12)
        self.port_cb = ctk.CTkComboBox(row, values=[""], variable=self.port_var, width=206)
        self.port_cb.pack(side="left")
        ctk.CTkButton(row, text="↻", width=36, command=self.refresh_ports).pack(side="left", padx=(6, 0))
        self.btn_conn = ctk.CTkButton(p, text="KẾT NỐI", height=36, command=self.toggle_conn)
        self.btn_conn.pack(fill="x", padx=12, pady=(8, 2))
        self.lbl_conn = ctk.CTkLabel(p, text="● Chưa kết nối", text_color="#f85149")
        self.lbl_conn.pack(anchor="w", padx=14)

        self.section(p, "TÌNH TRẠNG BÃI")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x", padx=8)
        self.cards = {}
        defs = [("free", "CHỖ TRỐNG", "#3fb950"), ("parked", "ĐANG ĐỖ", "#f0883e"),
                ("vip", "VIP TRONG BÃI", "#f5b301"), ("in", "LƯỢT VÀO", "#58a6ff"),
                ("out", "LƯỢT RA", "#a371f7"), ("rev", "DOANH THU (VNĐ)", "#3fb950")]
        for i, (k, t, col) in enumerate(defs):
            f = ctk.CTkFrame(grid, fg_color="#161b22", corner_radius=10)
            f.grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="nsew")
            ctk.CTkLabel(f, text=t, font=ctk.CTkFont("Segoe UI", 10), text_color="#8b949e").pack(pady=(6, 0))
            lb = ctk.CTkLabel(f, text="0", font=ctk.CTkFont("Segoe UI", 26, "bold"), text_color=col)
            lb.pack(pady=(0, 6), padx=10)
            self.cards[k] = lb
        grid.grid_columnconfigure((0, 1), weight=1)

        self.section(p, "AN TOÀN & TRẠNG THÁI")
        self.ind_fire = ctk.CTkLabel(p, text="CHÁY: BÌNH THƯỜNG", height=32, corner_radius=8, fg_color="#14301d")
        self.ind_fire.pack(fill="x", padx=12, pady=2)
        self.ind_gas = ctk.CTkLabel(p, text="GAS: BÌNH THƯỜNG", height=32, corner_radius=8, fg_color="#14301d")
        self.ind_gas.pack(fill="x", padx=12, pady=2)
        self.ind_admin = ctk.CTkLabel(p, text="ADMIN: TẮT", height=32, corner_radius=8, fg_color="#1f2933")
        self.ind_admin.pack(fill="x", padx=12, pady=2)

        self.section(p, "ĐIỀU KHIỂN THỦ CÔNG")
        controls = ctk.CTkFrame(p, fg_color="transparent")
        controls.pack(fill="x", padx=12, pady=3)
        controls.grid_columnconfigure((0, 1), weight=1)
        for row, gate in enumerate(("IN", "OUT")):
            name = "VÀO" if gate == "IN" else "RA"
            ctk.CTkButton(controls, text=f"MỞ CỔNG {name}", width=120, height=34,
                          command=lambda g=gate: self.manual(g, True)).grid(
                              row=row, column=0, padx=(0, 3), pady=3, sticky="ew")
            ctk.CTkButton(controls, text=f"ĐÓNG CỔNG {name}", width=120, height=34,
                          fg_color="#8957e5", command=lambda g=gate: self.manual(g, False)).grid(
                              row=row, column=1, padx=(3, 0), pady=3, sticky="ew")
        self.lbl_manual = ctk.CTkLabel(p, text="VÀO: ĐÓNG | RA: ĐÓNG", font=("Segoe UI", 11))
        self.lbl_manual.pack(fill="x", padx=12)
        ctk.CTkButton(p, text="ĐỒNG BỘ GIỜ PC → ESP32", height=34, fg_color="#30363d", command=self.sync_time).pack(fill="x", padx=12, pady=3)
        self.btn_demo = ctk.CTkButton(p, text="CHẠY DEMO (không cần ESP 32)", height=34, fg_color="#9e6a03", command=self.toggle_demo)
        self.btn_demo.pack(fill="x", padx=12, pady=(10, 3))

    def build_center(self, p):
        self.cv = tk.Canvas(p, width=CW, height=CH, bg="#0d1117", highlightthickness=0)
        self.cv.pack(padx=10, pady=(10, 4))
        self.build_scene()

        ctk.CTkLabel(p, text="XE ĐANG TRONG BÃI", font=ctk.CTkFont("Segoe UI", 12, "bold"), text_color="#8b949e").pack(anchor="w", padx=14)
        self.setup_style()
        cols = ("slot", "uid", "type", "tin", "dur", "fee")
        self.tbl = ttk.Treeview(p, columns=cols, show="headings", height=4)
        for c, t, w in zip(cols, ("Ô", "UID", "Loại", "Giờ vào", "Đã đỗ", "Phí tạm tính"), (50, 120, 90, 150, 110, 140)):
            self.tbl.heading(c, text=t)
            self.tbl.column(c, width=w, anchor="center")
        self.tbl.pack(fill="x", padx=10, pady=(2, 8))

    def build_right(self, p):
        ctk.CTkLabel(p, text="LỊCH SỬ QUÉT THẺ & SỰ KIỆN", font=ctk.CTkFont("Segoe UI", 14, "bold")).pack(anchor="w", padx=14, pady=(14, 6))
        cols = ("time", "gate", "uid", "type", "detail")
        fr = ctk.CTkFrame(p, fg_color="transparent")
        fr.pack(fill="both", expand=True, padx=10)
        self.log_tree = ttk.Treeview(fr, columns=cols, show="headings")
        for c, t, w in zip(cols, ("Thời gian", "Cổng", "UID", "Loại", "Chi tiết"), (140, 60, 90, 70, 260)):
            self.log_tree.heading(c, text=t)
            self.log_tree.column(c, width=w, anchor="w" if c == "detail" else "center")
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_tree.pack(side="left", fill="both", expand=True)
        for tag, col in (("IN", "#3fb950"), ("OUT", "#58a6ff"), ("DENY", "#f85149"),
                         ("ALARM", "#f0883e"), ("MAN", "#d2a8ff"), ("SYS", "#8b949e")):
            self.log_tree.tag_configure(tag, foreground=col)
        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=10)
        ctk.CTkButton(bar, text="Xuất CSV", width=110, command=self.export_csv).pack(side="left")
        ctk.CTkButton(bar, text="Xoá hiển thị", width=110, fg_color="#30363d", command=lambda: self.log_tree.delete(*self.log_tree.get_children())).pack(side="left", padx=8)

    def setup_style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background="#161b22", fieldbackground="#161b22", foreground="#e6edf3",
                     rowheight=26, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background="#21262d", foreground="#e6edf3",
                     font=("Segoe UI", 10, "bold"), relief="flat")
        st.map("Treeview", background=[("selected", "#1f6feb")])

    # ------------------------------------------------------------------ MÔ HÌNH
    def build_scene(self):
        cv = self.cv
        cv.create_rectangle(120, 44, 700, 380, fill="#161d27", outline="")
        cv.create_rectangle(0, 270, CW, 350, fill="#2b313b", outline="")
        cv.create_line(0, LANE_Y, CW, LANE_Y, fill="#5b6572", dash=(16, 14), width=2)
        for pts in ((120, 44, 700, 44), (120, 44, 120, 270), (120, 350, 120, 380),
                    (700, 44, 700, 270), (700, 350, 700, 380), (120, 380, 700, 380)):
            cv.create_line(*pts, fill="#8b98a8", width=5)
        cv.create_text(58, 252, text="CỔNG VÀO ▶", fill="#3fb950", font=("Segoe UI", 11, "bold"))
        cv.create_text(762, 252, text="CỔNG RA ▶", fill="#58a6ff", font=("Segoe UI", 11, "bold"))
        self.lot_txt = cv.create_text(410, 58, text="", fill="#e6edf3", font=("Segoe UI", 12, "bold"))

        self.slot_rect, self.slot_txt = [], []
        for i, sx in enumerate(SLOT_X):
            self.slot_rect.append(cv.create_rectangle(sx - 55, SLOT_Y1, sx + 55, SLOT_Y2, fill="#12261a", outline="#3fb950", width=2))
            cv.create_text(sx, SLOT_Y1 + 14, text=f"P{i + 1}", fill="#8b949e", font=("Segoe UI", 11, "bold"))
            self.slot_txt.append(cv.create_text(sx, SLOT_CY, text="TRỐNG", fill="#3fb950", font=("Segoe UI", 11, "bold")))

        # LCD mô phỏng
        cv.create_rectangle(240, 410, 580, 510, fill="#10151c", outline="#30363d", width=2)
        cv.create_rectangle(256, 424, 564, 496, fill="#2f6b3a", outline="#1b3d22", width=2)
        self.lcd1 = cv.create_text(268, 444, anchor="w", text="", fill="#d8ffd0", font=("Consolas", 17, "bold"))
        self.lcd2 = cv.create_text(268, 476, anchor="w", text="", fill="#d8ffd0", font=("Consolas", 17, "bold"))
        cv.create_text(410, 524, text="LCD 16x2 (mô phỏng)", fill="#6e7681", font=("Segoe UI", 9))

        self.bar_in = Barrier(cv, GATE_IN_X, 352, -180)
        self.bar_out = Barrier(cv, GATE_OUT_X, 352, 0)

        self.ov_fire = cv.create_rectangle(120, 44, 700, 380, fill="#ff2d2d", stipple="gray25", outline="", state="hidden", tags=("overlay",))
        self.ov_gas = cv.create_rectangle(120, 44, 700, 380, fill="#ffcc00", stipple="gray25", outline="", state="hidden", tags=("overlay",))
        self.banner = cv.create_rectangle(0, 0, CW, 30, fill="#b62324", outline="", state="hidden", tags=("overlay",))
        self.banner_txt = cv.create_text(CW / 2, 15, text="", fill="white", font=("Segoe UI", 13, "bold"), state="hidden", tags=("overlay",))

    def raise_layers(self):
        self.cv.tag_raise("barrier")
        self.cv.tag_raise("overlay")

    def spawn_car(self, uid, vip, x, y, heading):
        car = Car(self.cv, uid, vip, x, y, heading)
        self.cars.append(car)
        self.raise_layers()
        return car

    def remove_car(self, car):
        if car in self.cars:
            self.cars.remove(car)
        car.delete()
        for i, c in enumerate(self.slot_cars):
            if c is car:
                self.slot_cars[i] = None

    def float_text(self, x, y, text, color, ms=2200):
        t = self.cv.create_text(x, y, text=text, fill=color, font=("Segoe UI", 12, "bold"))
        self.cv.tag_raise(t)
        self.after(ms, lambda: self.cv.delete(t))

    def update_slots_visual(self):
        free = 0
        for i in range(MAX_CARS):
            car = self.slot_cars[i]
            occ = self.slots[i] is not None and car is not None and not car.moving()
            reserved = self.slots[i] is not None
            if not reserved:
                free += 1
            self.cv.itemconfig(self.slot_rect[i], fill="#2a1518" if reserved else "#12261a",
                               outline="#f85149" if reserved else "#3fb950")
            self.cv.itemconfig(self.slot_txt[i], text="" if reserved else "TRỐNG")
        self.cv.itemconfig(self.lot_txt, text=f"CHỖ TRỐNG: {free}/{MAX_CARS}")
        return free

    def anim_tick(self):
        self.frame += 1
        self.bar_in.step()
        self.bar_out.step()
        for car in list(self.cars):
            car.step()
        # nhấp nháy cảnh báo
        blink = (self.frame // 10) % 2 == 0
        if self.fire:
            self.cv.itemconfig(self.ov_fire, state="normal" if blink else "hidden")
            self.cv.itemconfig(self.banner, fill="#b62324", state="normal")
            self.cv.itemconfig(self.banner_txt, text="PHÁT HIỆN CHÁY  -  ĐÃ MỞ 2 BARRIER THOÁT HIỂM", state="normal")
        elif self.gas:
            self.cv.itemconfig(self.ov_fire, state="hidden")
            self.cv.itemconfig(self.ov_gas, state="normal" if blink else "hidden")
            self.cv.itemconfig(self.banner, fill="#9e6a03", state="normal")
            self.cv.itemconfig(self.banner_txt, text="CẢNH BÁO RÒ RỈ KHÍ GAS  -  KHOÁ QUẸT THẺ", state="normal")
        else:
            for it in (self.ov_fire, self.ov_gas, self.banner, self.banner_txt):
                self.cv.itemconfig(it, state="hidden")
        if (self.fire or self.gas) and winsound and self.frame % 30 == 0:
            threading.Thread(target=lambda: winsound.Beep(1200 if self.fire else 800, 180), daemon=True).start()
        self.raise_layers()
        self.after(30, self.anim_tick)

    # ------------------------------------------------------------------ LCD mô phỏng
    def set_lcd(self, l1, l2):
        self.cv.itemconfig(self.lcd1, text=l1[:16])
        self.cv.itemconfig(self.lcd2, text=l2[:16])

    def lcd_msg(self, l1, l2, ms=3500):
        self.lcd_hold = time.monotonic() + ms / 1000
        self.set_lcd(l1, l2)

    def lcd_idle(self):
        s = self.stat
        if self.fire:
            self.set_lcd("!!! CHAY !!!", "DANG MO CONG...")
        elif self.gas:
            self.set_lcd("! CANH BAO GAS !", "NGUY HIEM !!!")
        elif self.admin:
            self.set_lcd("ADMIN MODE", "QUET THE: VIP")
        else:
            vc = s["vip_cnt"]
            l2 = "Slot VIP   :0/0" if vc == 0 else f"Slot VIP   :{s['vip_parked']}/{vc}"
            self.set_lcd(f"Slot Thuong:{s['parked'] - s['vip_parked']}/{s['max'] - vc}", l2)

    # ------------------------------------------------------------------ SERIAL
    def refresh_ports(self):
        vals = [f"{p.device} - {p.description}" for p in list_ports.comports()] or ["(không có cổng)"]
        self.port_cb.configure(values=vals)
        if not self.port_var.get() or self.port_var.get() not in vals:
            self.port_var.set(vals[0])

    def set_conn_label(self, text, color):
        self.lbl_conn.configure(text=text, text_color=color)

    def toggle_conn(self):
        if self.ser:
            self.disconnect()
            return
        if self.demo:
            self.toggle_demo()
        port = self.port_var.get().split(" ")[0]
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0.1)
        except Exception as e:
            messagebox.showerror("Lỗi kết nối", f"Không mở được {port}\n{e}")
            self.ser = None
            return
        self.running = True
        threading.Thread(target=self.reader, args=(self.ser,), daemon=True).start()
        self.btn_conn.configure(text="NGẮT KẾT NỐI", fg_color="#da3633")
        self.set_conn_label(f"● Đã kết nối {port}", "#3fb950")
        self.last_rx = time.monotonic()
        self.reset_state()
        self.reset_gate_view()
        self.fire = self.gas = self.admin = False
        self.set_ind(self.ind_fire, False, "CHÁY: BÌNH THƯỜNG", "", "#b62324")
        self.set_ind(self.ind_gas, False, "GAS: BÌNH THƯỜNG", "", "#9e6a03")
        self.ind_admin.configure(text="ADMIN: TẮT", fg_color="#1f2933")
        self.log("SYS", "", "", "", f"Kết nối {port} @ {BAUD}")
        self.after(2200, lambda: self.send("S"))  # ESP 32 reset khi mở cổng, chờ boot rồi xin trạng thái

    def disconnect(self):
        self.running = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.btn_conn.configure(text="KẾT NỐI", fg_color=["#3a7ebf", "#1f538d"])
        self.set_conn_label("● Chưa kết nối", "#f85149")
        self.log("SYS", "", "", "", "Ngắt kết nối")

    def reader(self, ser):
        buf = b""
        while self.running:
            try:
                data = ser.read(128)
            except Exception:
                if self.running:
                    self.q.put(None)
                return
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                txt = line.decode("utf-8", "ignore").strip()
                if txt:
                    self.q.put(txt)

    def send(self, cmd):
        if not self.ser:
            return False
        try:
            self.ser.write((cmd + "\n").encode())
            return True
        except Exception:
            return False

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self.disconnect()
                    messagebox.showwarning("USB", "Mất kết nối với ESP 32.")
                else:
                    self.last_rx = time.monotonic()
                    self.handle_line(item)
        except queue.Empty:
            pass
        self.after(40, self.poll)

    # ------------------------------------------------------------------ XỬ LÝ GÓI TIN
    def handle_line(self, line):
        p = line.strip().split("|")
        k = p[0]
        try:
            if k == "TIME":
                self.rtc_unix, self.rtc_mono = int(p[1]), time.monotonic()
            elif k == "STAT":
                self.on_stat(p)
            elif k == "EVT" and p[1] == "IN":
                self.on_in(p[2], p[3] == "V", int(p[4]), int(p[5]))
            elif k == "EVT" and p[1] == "OUT":
                self.on_out(p[2], p[3] == "V", int(p[4]), int(p[5]), int(p[6]))
            elif k == "DENY":
                self.on_deny(p[1], p[2], p[3])
            elif k == "GATE":
                gate, opened = p[1], p[2] == "1"
                if gate in self.gate_open:
                    self.gate_open[gate] = opened
                    (self.bar_in if gate == "IN" else self.bar_out).set_open(opened)
                    self.update_gate_status()
            elif k == "HOLD":
                if p[1] in self.manual_hold:
                    self.manual_hold[p[1]] = p[2] == "1"
                    self.update_gate_status()
            elif k == "ALARM":
                self.on_alarm(p[1], p[2] == "1")
            elif k == "ADMIN":
                self.on_admin(p[1:])
            elif k == "MAN":
                opened = len(p) < 3 or p[2] != "0"
                self.manual_hold[p[1]] = opened
                self.update_gate_status()
                self.log("MAN", "VÀO" if p[1] == "IN" else "RA", "", "",
                         "Mở barrier thủ công" if opened else "Đóng barrier thủ công")
            elif k == "ESP" and p[1] == "COUNT":
                self.counts = dict(IN=int(p[2]), OUT=int(p[3]), rev=int(p[4]))
                self.update_counters()
            elif k == "ESP" and p[1] == "CMD" and p[2] == "BUSY":
                messagebox.showwarning("Không thể điều khiển", "ESP32 đang báo cháy hoặc ở chế độ quản trị.")
            elif k == "SYS":
                self.log("SYS", "", "", "", "ESP 32 sẵn sàng")
            else:
                print("RX:", line)
        except (ValueError, IndexError):
            print("Gói lỗi:", line)

    def on_stat(self, p):
        parked, vp, vc, mx = (int(x) for x in p[1:5])
        self.stat = dict(parked=parked, vip_parked=vp, vip_cnt=vc, max=mx)
        info = [None] * MAX_CARS
        for i, tok in enumerate(p[5].split(",")[:MAX_CARS]):
            uid, ts, v = tok.split(":")
            if uid != "00000000":
                info[i] = dict(uid=uid, ts=int(ts), vip=v == "1")
        if not any(c.moving() for c in self.cars):
            for i in range(MAX_CARS):
                cur, car = info[i], self.slot_cars[i]
                if cur and (car is None or car.uid != cur["uid"]):
                    if car:
                        self.remove_car(car)
                    car = self.spawn_car(cur["uid"], cur["vip"], SLOT_X[i], SLOT_CY, -90)
                    self.slot_cars[i] = car
                elif not cur and car:
                    self.remove_car(car)
                self.slots[i] = cur
        self.update_counters()
        self.update_slots_visual()

    def on_in(self, uid, vip, slot, ts):
        self.slots[slot] = dict(uid=uid, ts=ts, vip=vip)
        self.counts["IN"] += 1
        self.log("IN", "VÀO", uid, "VIP" if vip else "Thường", f"Cho vào - ô P{slot + 1}")
        if self.slot_cars[slot]:
            self.remove_car(self.slot_cars[slot])
        car = self.spawn_car(uid, vip, -80, LANE_Y, 0)
        car.speed = 7.0
        sx = SLOT_X[slot]
        car.path = [(sx - 48, LANE_Y), (sx, LANE_Y - 46), (sx, SLOT_CY)]
        car.on_done = lambda c=car: self.car_parked(c)
        self.slot_cars[slot] = car
        self.lcd_msg("VIP (VAO)" if vip else "XE THUONG (VAO)", fmt_dt(ts)[:5] + " " + fmt_dt(ts)[9:])
        self.update_counters()
        self.update_slots_visual()

    def car_parked(self, car):
        car.heading = -90
        car.draw()
        self.update_slots_visual()

    def on_out(self, uid, vip, slot, dur, fee):
        car = self.slot_cars[slot]
        if car is None or car.uid != uid:
            if car:
                self.remove_car(car)
            car = self.spawn_car(uid, vip, SLOT_X[slot], SLOT_CY, -90)
        self.slot_cars[slot] = None
        self.slots[slot] = None
        sx = SLOT_X[slot]
        car.speed = 11.0
        car.path = [(sx, 262, "r"), (sx + 34, LANE_Y), (CW + 90, LANE_Y)]
        car.on_done = lambda c=car: self.remove_car(c)
        self.counts["OUT"] += 1
        self.counts["rev"] += fee
        self.log("OUT", "RA", uid, "VIP" if vip else "Thường", f"Ô P{slot + 1} | {fmt_dur(dur)} | " + ("miễn phí" if vip else f"{fee:,} VNĐ"), fee)
        if vip:
            self.lcd_msg("VIP: MIEN PHI", f"TG: {dur / 60:.1f}p")
        else:
            self.lcd_msg(f"TG : {dur / 60:.1f} phut", f"PHI: {fee} VND")
        self.update_counters()
        self.update_slots_visual()

    def on_deny(self, gate, uid, reason):
        texts = {"FULL": "HẾT CHỖ", "DUP": "XE ĐÃ TRONG BÃI", "NOTIN": "XE CHƯA VÀO BÃI", "ADMIN": "THẺ ADMIN CHỈ DÙNG Ở CỔNG VÀO"}
        txt = texts.get(reason, reason)
        self.log("DENY", "VÀO" if gate == "IN" else "RA", uid, "", "Từ chối: " + txt)
        self.lcd_msg("TU CHOI", reason, 2500)
        if gate == "IN":
            car = self.spawn_car(uid, False, -80, LANE_Y, 0)
            stop, away = (58, LANE_Y), (-100, LANE_Y, "r")
            tx = 58
        else:
            car = self.spawn_car(uid, False, CW + 80, LANE_Y, 180)
            stop, away = (765, LANE_Y), (CW + 100, LANE_Y, "r")
            tx = 765
        car.speed = 7.0
        car.path = [stop]

        def arrived(c=car):
            self.float_text(tx, LANE_Y - 50, txt, "#f85149")
            self.after(1700, lambda: leave(c))

        def leave(c):
            if c in self.cars:
                c.path = [away]
                c.on_done = lambda: self.remove_car(c)

        car.on_done = arrived

    def on_alarm(self, kind, on):
        if kind == "FIRE":
            self.fire = on
            self.set_ind(self.ind_fire, on, "CHÁY: BÌNH THƯỜNG", "!!! PHÁT HIỆN CHÁY !!!", "#b62324")
            self.log("ALARM", "", "", "", "Phát hiện cháy - mở 2 cổng" if on else "Hết cháy")
        else:
            self.gas = on
            self.set_ind(self.ind_gas, on, "GAS: BÌNH THƯỜNG", "!!! RÒ RỈ KHÍ GAS !!!", "#9e6a03")
            self.log("ALARM", "", "", "", "Cảnh báo khí gas - khoá quẹt thẻ" if on else "Hết cảnh báo gas")
        self.lcd_hold = 0

    def set_ind(self, lb, on, ok, bad, color):
        lb.configure(text=bad if on else ok, fg_color=color if on else "#14301d")

    def on_admin(self, a):
        if a[0] in ("1", "0"):
            self.admin = a[0] == "1"
            self.ind_admin.configure(text="ADMIN: ĐANG HOẠT ĐỘNG" if self.admin else "ADMIN: TẮT",
                                     fg_color="#1f6feb" if self.admin else "#1f2933")
            self.log("SYS", "VÀO", "", "", "Vào chế độ Admin" if self.admin else "Thoát chế độ Admin")
        elif a[0] == "ADD":
            self.log("SYS", "VÀO", a[1], "VIP", "Thêm thẻ VIP")
        elif a[0] == "DEL":
            self.log("SYS", "VÀO", a[1], "VIP", "Xoá thẻ VIP")
        elif a[0] == "RESET":
            self.log("SYS", "VÀO", "", "", "Admin reset toàn bộ dữ liệu")
            self.reset_state()
        self.lcd_hold = 0

    def reset_state(self):
        for c in list(self.cars):
            self.remove_car(c)
        self.slots = [None] * MAX_CARS
        self.slot_cars = [None] * MAX_CARS
        self.stat = dict(parked=0, vip_parked=0, vip_cnt=0, max=MAX_CARS)
        self.update_counters()
        self.update_slots_visual()

    # ------------------------------------------------------------------ SỐ LIỆU & BẢNG
    def update_counters(self):
        s = self.stat
        free = max(0, s["max"] - s["parked"])
        self.cards["free"].configure(text=str(free), text_color="#3fb950" if free else "#f85149")
        self.cards["parked"].configure(text=f"{s['parked']}/{s['max']}")
        self.cards["vip"].configure(text=f"{s['vip_parked']}/{s['vip_cnt']}")
        self.cards["in"].configure(text=str(self.counts["IN"]))
        self.cards["out"].configure(text=str(self.counts["OUT"]))
        self.cards["rev"].configure(text=f"{self.counts['rev']:,}")

    def board_now(self):
        if self.rtc_unix:
            return self.rtc_unix + int(time.monotonic() - self.rtc_mono)
        return local_epoch()

    def refresh_parked_table(self):
        self.tbl.delete(*self.tbl.get_children())
        now = self.board_now()
        for i, s in enumerate(self.slots):
            if s:
                el = now - s["ts"]
                fee = "Miễn phí" if s["vip"] else f"{el:,} VNĐ"
                self.tbl.insert("", "end", values=(f"P{i + 1}", s["uid"], "VIP" if s["vip"] else "Thường", fmt_dt(s["ts"]), fmt_dur(el), fee))
            else:
                self.tbl.insert("", "end", values=(f"P{i + 1}", "—", "—", "—", "—", "—"))

    def sec_tick(self):
        self.lbl_pc.configure(text="PC: " + datetime.now().strftime("%H:%M:%S %d/%m/%Y"))
        if self.rtc_unix:
            self.lbl_rtc.configure(text="ESP32: " + datetime.fromtimestamp(self.board_now(), timezone.utc).strftime("%H:%M:%S %d/%m/%Y"))
        self.refresh_parked_table()
        if time.monotonic() > self.lcd_hold:
            self.lcd_idle()
        if self.ser and time.monotonic() - self.last_rx > 8:
            self.set_conn_label("● Không nhận được dữ liệu", "#d29922")
        self.after(1000, self.sec_tick)

    # ------------------------------------------------------------------ LỊCH SỬ
    def log(self, kind, gate, uid, typ, detail, fee=0, save=True):
        ts = datetime.now().strftime("%d/%m %H:%M:%S")
        self.log_tree.insert("", 0, values=(ts, gate, uid, typ, detail), tags=(kind,))
        if save:
            new = not os.path.exists(CSV_FILE)
            try:
                with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["time", "kind", "gate", "uid", "type", "detail", "fee"])
                    w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, gate, uid, typ, detail, fee])
            except OSError:
                pass

    def load_history(self):
        if not os.path.exists(CSV_FILE):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))[1:]
        except OSError:
            return
        for r in rows:
            if len(r) < 7:
                continue
            if r[0].startswith(today):
                if r[1] == "IN":
                    self.counts["IN"] += 1
                elif r[1] == "OUT":
                    self.counts["OUT"] += 1
                    try:
                        self.counts["rev"] += int(r[6])
                    except ValueError:
                        pass
        for r in rows[-300:]:
            if len(r) >= 7:
                try:
                    t = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M:%S")
                except ValueError:
                    t = r[0]
                self.log_tree.insert("", 0, values=(t, r[2], r[3], r[4], r[5]), tags=(r[1],))

    def export_csv(self):
        if not os.path.exists(CSV_FILE):
            messagebox.showinfo("Xuất CSV", "Chưa có dữ liệu lịch sử.")
            return
        dst = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="lich_su_bai_do_xe.csv")
        if dst:
            shutil.copy(CSV_FILE, dst)

    # ------------------------------------------------------------------ ĐIỀU KHIỂN
    def sync_time(self):
        if self.demo or not self.ser:
            messagebox.showinfo("Đồng bộ giờ", "Cần kết nối ESP 32 thật.")
            return
        self.send(f"T{local_epoch()}")
        self.log("SYS", "", "", "", "Gửi giờ PC xuống ESP32")

    def update_gate_status(self):
        parts = []
        for gate, label in (("IN", "VÀO"), ("OUT", "RA")):
            state = "ĐÓNG"
            if self.gate_open[gate]:
                state = "MỞ TAY" if self.manual_hold[gate] else "MỞ"
            parts.append(f"{label}: {state}")
        self.lbl_manual.configure(text=" | ".join(parts))

    def manual(self, gate, opened=True):
        if self.fire or self.admin:
            messagebox.showwarning("Không thể điều khiển", "Đang báo cháy hoặc ở chế độ quản trị.")
            return
        if self.demo:
            self.cancel_demo_gate(gate)
            self.handle_line(f"HOLD|{gate}|{int(opened)}")
            self.handle_line(f"GATE|{gate}|{int(opened)}")
            self.handle_line(f"MAN|{gate}|{int(opened)}")
            return
        if not self.ser:
            messagebox.showwarning("Chưa kết nối", "Hãy kết nối ESP32 qua USB trước.")
            return
        command = "I" if gate == "IN" else "O"
        if not self.send(command if opened else command.lower()):
            messagebox.showwarning("USB", "Không gửi được lệnh đến ESP32.")

    # ------------------------------------------------------------------ DEMO
    def toggle_demo(self):
        if self.ser:
            self.disconnect()
        self.demo = not self.demo
        self.cancel_demo_jobs()
        self.reset_gate_view()
        self.fire = self.gas = self.admin = False
        self.set_ind(self.ind_fire, False, "CHÁY: BÌNH THƯỜNG", "", "#b62324")
        self.set_ind(self.ind_gas, False, "GAS: BÌNH THƯỜNG", "", "#9e6a03")
        self.ind_admin.configure(text="ADMIN: TẮT", fg_color="#1f2933")
        if self.demo:
            self.d_slots = [None] * MAX_CARS
            self.reset_state()
            self.btn_demo.configure(text="DỪNG DEMO")
            self.set_conn_label("● Chế độ DEMO", "#d29922")
            self.demo_step()
        else:
            self.btn_demo.configure(text="CHẠY DEMO (không cần ESP 32)")
            self.set_conn_label("● Chưa kết nối", "#f85149")

    def reset_gate_view(self):
        self.manual_hold = {"IN": False, "OUT": False}
        self.gate_open = {"IN": False, "OUT": False}
        self.bar_in.set_open(False)
        self.bar_out.set_open(False)
        self.update_gate_status()

    def cancel_demo_gate(self, gate):
        job = self.demo_gate_jobs[gate]
        if job is not None:
            self.after_cancel(job)
            self.demo_gate_jobs[gate] = None

    def cancel_demo_jobs(self):
        for gate in ("IN", "OUT"):
            self.cancel_demo_gate(gate)
        for attr in ("demo_step_job", "demo_fire_job"):
            job = getattr(self, attr)
            if job is not None:
                self.after_cancel(job)
                setattr(self, attr, None)

    def demo_gate(self, gate):
        self.cancel_demo_gate(gate)
        if not self.demo:
            return
        self.handle_line(f"GATE|{gate}|1")
        if not self.manual_hold[gate]:
            self.demo_gate_jobs[gate] = self.after(3000, lambda: self.demo_auto_close(gate))

    def demo_auto_close(self, gate):
        self.demo_gate_jobs[gate] = None
        if self.demo and not self.fire and not self.manual_hold[gate]:
            self.handle_line(f"GATE|{gate}|0")

    def demo_end_fire(self):
        self.demo_fire_job = None
        if not self.demo:
            return
        self.handle_line("ALARM|FIRE|0")
        for gate in ("IN", "OUT"):
            self.handle_line(f"GATE|{gate}|{int(self.manual_hold[gate])}")

    def demo_stat(self):
        ds = self.d_slots
        n = sum(1 for s in ds if s)
        vp = sum(1 for s in ds if s and s["vip"])
        toks = [f"{s['uid']}:{s['ts']}:{1 if s['vip'] else 0}" if s else "00000000:0:0" for s in ds]
        return f"STAT|{n}|{vp}|{max(vp, 1)}|{MAX_CARS}|" + ",".join(toks)

    def demo_step(self):
        self.demo_step_job = None
        if not self.demo:
            return
        if self.fire or self.gas or self.admin:
            self.demo_step_job = self.after(1000, self.demo_step)
            return
        now = local_epoch()
        ds = self.d_slots
        free = [i for i, s in enumerate(ds) if s is None]
        occ = [i for i, s in enumerate(ds) if s]
        r = random.random()
        if r < 0.05 and not self.fire:
            for x in ("ALARM|FIRE|1", "GATE|IN|1", "GATE|OUT|1"):
                self.handle_line(x)
            for gate in ("IN", "OUT"):
                self.cancel_demo_gate(gate)
            self.demo_fire_job = self.after(4500, self.demo_end_fire)
        elif r < 0.11:
            self.handle_line(f"DENY|OUT|{rand_uid()}|NOTIN")
        elif r < 0.17 and not free:
            self.handle_line(f"DENY|IN|{rand_uid()}|FULL")
        elif free and (not occ or r < 0.62):
            i = free[0]
            uid, vip = rand_uid(), random.random() < 0.3
            ts = now - random.randint(0, 900)
            ds[i] = dict(uid=uid, ts=ts, vip=vip)
            self.handle_line(f"EVT|IN|{uid}|{'V' if vip else 'N'}|{i}|{ts}")
            self.demo_gate("IN")
            self.handle_line(self.demo_stat())
        elif occ:
            i = random.choice(occ)
            s = ds[i]
            dur = max(1, now - s["ts"])
            fee = 0 if s["vip"] else dur
            ds[i] = None
            self.handle_line(f"EVT|OUT|{s['uid']}|{'V' if s['vip'] else 'N'}|{i}|{dur}|{fee}")
            self.demo_gate("OUT")
            self.handle_line(self.demo_stat())
        self.demo_step_job = self.after(random.randint(4500, 7000), self.demo_step)

    # ------------------------------------------------------------------
    def on_close(self):
        self.running = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
