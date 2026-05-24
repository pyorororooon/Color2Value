import tkinter as tk
import tkinter.font as tkfont
import numpy as np
import mss
from PIL import Image, ImageTk, ImageOps
import keyboard
import threading
import sys
import os
import webbrowser

def resource_path(relative_path):
    """PyInstaller (_MEIPASS) と通常実行の両方に対応したリソースパスを返す"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)

class ColorPickerApp:
    _TICK_MIN_GAP = 3
    _TICK_MAX_ZONE = 48
    _DONATION_URL = "https://buymeacoffee.com/pyorororo0224"

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw() # メインウィンドウは常に隠しておく

        # 状態管理変数
        self.overlays = []          # [(Toplevel, Canvas, ImageTk, ox, oy), ...]
        self.canvas = None          # 現在操作中のキャンバス
        self.canvas_offset = (0, 0) # そのキャンバスの仮想スクリーン内オフセット
        self.image_array = None
        self.colorbar_array = None
        self.colorbar_lab   = None
        self.colorbar_bbox = None
        self.rect_id = None
        self.start_x = None
        self.start_y = None
        self.is_vertical = False
        self.mode = "wait" # "wait", "select_bar", "pick_color"
        self.mag_tk_img = None  # 拡大鏡用PhotoImage（GC対策）
        self._toast_window   = None
        self.value_map       = None  # [(frac, value), ...] キャリブレーション後に設定
        self._calib_region   = None  # 再検出用に選択領域を保持
        self._easyocr_reader = None  # 遅延初期化
        self._colorbar_canvas = None  # カラーバーを描画したキャンバス

        # ホットキー: Ctrl+F9（左右どちらのCtrlでも反応、Altを使わない）
        self._ctrl_pressed = False
        keyboard.on_press(self._on_key_press)
        keyboard.on_release(self._on_key_release)
        print("Ready. Press [Ctrl + F9] to capture the screen.")
        self.root.after(200, self._show_welcome_image)
        self._setup_tray()

    def _setup_tray(self):
        """システムトレイアイコンを設定してバックグラウンドで起動"""
        try:
            import pystray
        except ImportError:
            print("pystray not installed: pip install pystray")
            return

        icon_img = Image.open(resource_path("Color2Value.ico"))

        menu = pystray.Menu(
            pystray.MenuItem('Color Picker', None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Capture Screen  (Ctrl+F9)',
                             lambda icon, item: self.root.after(0, self.start_capture)),
            pystray.MenuItem('Show Help',
                             lambda icon, item: self.root.after(0, self._show_welcome_image)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit',
                             lambda icon, item: self.root.after(0, self.root.destroy)),
        )

        self._tray_icon = pystray.Icon('ColorPicker', icon_img, 'Color Picker', menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _show_welcome_image(self):
        """起動時チュートリアル画像をスクリーン面積の1/4サイズで中央表示"""
        img_path = resource_path("Sea Surface Salinity.jpg")
        try:
            img = Image.open(img_path)
        except Exception:
            return  # 画像がなければスキップ

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        iw, ih = img.size
        scale = ((sw * sh / 4) / (iw * ih)) ** 0.5
        nw, nh = int(iw * scale), int(ih * scale)
        img = img.resize((nw, nh), Image.LANCZOS)

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.geometry(f"{nw}x{nh}+{(sw - nw) // 2}+{(sh - nh) // 2}")

        tk_img = ImageTk.PhotoImage(img)
        cv = tk.Canvas(win, width=nw, height=nh, highlightthickness=0)
        cv.pack()
        img_id = cv.create_image(0, 0, anchor=tk.NW, image=tk_img)
        cv._img = tk_img  # GC防止

        donation_top = int(nh * 4 / 5)  # 画像下1/5のみを寄付リンク領域にする
        drag_state = {
            "press_x": 0,
            "press_y": 0,
            "win_x": 0,
            "win_y": 0,
            "moved": False,
        }

        BTN = 32
        bx, by = nw - BTN - 10, 10
        bg_id = cv.create_rectangle(bx, by, bx + BTN, by + BTN,
                                    fill="#1f2937", outline="#4b5563", width=1)
        ic_id = cv.create_text(bx + BTN // 2, by + BTN // 2,
                               text="\u2715", fill="#9ca3af", font=("Segoe UI", 13))

        def close(e=None): win.destroy()
        def on_enter(e):
            cv.itemconfig(bg_id, fill="#7f1d1d"); cv.itemconfig(ic_id, fill="#fca5a5")
        def on_leave(e):
            cv.itemconfig(bg_id, fill="#1f2937"); cv.itemconfig(ic_id, fill="#9ca3af")
        for item in (bg_id, ic_id):
            cv.tag_bind(item, "<Button-1>", close)
            cv.tag_bind(item, "<Enter>",    on_enter)
            cv.tag_bind(item, "<Leave>",    on_leave)

        close_items = {bg_id, ic_id}

        def on_press(e):
            drag_state["press_x"] = e.x_root
            drag_state["press_y"] = e.y_root
            drag_state["win_x"] = win.winfo_x()
            drag_state["win_y"] = win.winfo_y()
            drag_state["moved"] = False

        def on_drag(e):
            dx = e.x_root - drag_state["press_x"]
            dy = e.y_root - drag_state["press_y"]
            if abs(dx) >= 3 or abs(dy) >= 3:
                drag_state["moved"] = True
            if drag_state["moved"]:
                win.geometry(f"+{drag_state['win_x'] + dx}+{drag_state['win_y'] + dy}")

        def on_release(e):
            if drag_state["moved"]:
                return
            hit = set(cv.find_overlapping(e.x - 1, e.y - 1, e.x + 1, e.y + 1))
            if hit & close_items:
                return
            if e.y >= donation_top:
                webbrowser.open_new_tab(self._DONATION_URL)

        def on_motion(e):
            hit = set(cv.find_overlapping(e.x - 1, e.y - 1, e.x + 1, e.y + 1))
            if hit & close_items:
                return
            if e.y >= donation_top:
                cv.config(cursor="hand2")
            else:
                cv.config(cursor="fleur")

        cv.bind("<ButtonPress-1>", on_press)
        cv.bind("<B1-Motion>", on_drag)
        cv.bind("<ButtonRelease-1>", on_release)
        cv.bind("<Motion>", on_motion)
        win.bind("<Escape>", close)

    def _on_key_press(self, event):
        if event.name in ('ctrl', 'left ctrl', 'right ctrl'):
            self._ctrl_pressed = True
        elif event.name == 'f9' and self._ctrl_pressed:
            self.root.after(0, self.start_capture)
        elif event.name == 'esc' and self.overlays:
            self.root.after(0, self.close_overlay)

    def _on_key_release(self, event):
        if event.name in ('ctrl', 'left ctrl', 'right ctrl'):
            self._ctrl_pressed = False

    def start_capture(self):
        if self.overlays:
            return
        threading.Thread(target=self._capture_background, daemon=True).start()

    def _capture_background(self):
        with mss.mss() as sct:
            vscreen = dict(sct.monitors[0])
            monitors = [dict(m) for m in sct.monitors[1:]]  # 実モニターのリスト
            sct_img = sct.grab(vscreen)
            img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
            image_array = np.array(img)
        self.root.after(0, lambda: self._show_overlay(img, image_array, vscreen, monitors))

    def _show_overlay(self, img, image_array, vscreen, monitors):
        if self.overlays:
            return
        self.image_array = image_array

        for mon in monitors:
            ox = mon['left'] - vscreen['left']
            oy = mon['top'] - vscreen['top']
            w, h = mon['width'], mon['height']

            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes('-topmost', True)
            ov.geometry(f"{w}x{h}{mon['left']:+d}{mon['top']:+d}")
            ov.config(cursor="crosshair")

            # このモニター分のスクリーンショットを切り出して表示
            slice_img = img.crop((ox, oy, ox + w, oy + h))
            cv = tk.Canvas(ov, highlightthickness=0)
            cv.pack(fill=tk.BOTH, expand=True)
            tk_img = ImageTk.PhotoImage(slice_img)
            cv.create_image(0, 0, anchor=tk.NW, image=tk_img)

            ov.bind("<Escape>", self.close_overlay)
            cv.bind("<Escape>", self.close_overlay)
            cv.bind("<ButtonPress-1>",   lambda e, c=cv, o=(ox, oy): self._cv_press(e, c, o))
            cv.bind("<B1-Motion>",       lambda e, c=cv, o=(ox, oy): self._cv_drag(e, c, o))
            cv.bind("<ButtonRelease-1>", lambda e, c=cv, o=(ox, oy): self._cv_release(e, c, o))
            cv.bind("<Motion>",          lambda e, c=cv: self._update_magnifier(e, c))

            self.overlays.append((ov, cv, tk_img, ox, oy))

        self.mode = "select_bar"
        self.show_message("Select Colorbar", "Drag to enclose bar incl. ticks & labels  •  Esc to cancel", 1)

    # ---- キャンバスイベント中継（モニターごとのオフセット対応） ----
    def _cv_press(self, event, canvas, offset):
        self.canvas = canvas
        self.canvas_offset = offset
        self.on_mouse_down(event)

    def _cv_drag(self, event, canvas, offset):
        self.canvas = canvas
        self.canvas_offset = offset
        self.on_mouse_drag(event)

    def _cv_release(self, event, canvas, offset):
        self.canvas = canvas
        self.canvas_offset = offset
        self.on_mouse_up(event)

    # --- 拡大鏡 ---
    _MAG_ZOOM = 8
    _MAG_RADIUS = 10

    def _update_magnifier(self, event, canvas):
        if self.mode != "pick_color":
            canvas.delete("magnifier")
            return
        ox, oy = 0, 0
        for _, cv, _, o0, o1 in self.overlays:
            if cv is canvas:
                ox, oy = o0, o1
                break
        x, y = event.x, event.y
        r = self._MAG_RADIUS
        h, w = self.image_array.shape[:2]

        x1, y1 = max(0, x + ox - r), max(0, y + oy - r)
        x2, y2 = min(w, x + ox + r + 1), min(h, y + oy + r + 1)
        patch = self.image_array[y1:y2, x1:x2]

        pw = (x2 - x1) * self._MAG_ZOOM
        ph = (y2 - y1) * self._MAG_ZOOM
        patch_img = Image.fromarray(patch.astype(np.uint8)).resize((pw, ph), Image.NEAREST)
        self.mag_tk_img = ImageTk.PhotoImage(patch_img)

        off = 24
        canvas_w = canvas.winfo_width()
        canvas_h = canvas.winfo_height()
        mx = x + off if x + off + pw < canvas_w else x - off - pw
        my = y + off if y + off + ph < canvas_h else y - off - ph

        canvas.delete("magnifier")
        canvas.create_rectangle(mx-3, my-3, mx+pw+3, my+ph+3, outline="black", width=3, tags="magnifier")
        canvas.create_rectangle(mx-2, my-2, mx+pw+2, my+ph+2, outline="white", width=2, tags="magnifier")
        canvas.create_image(mx, my, anchor=tk.NW, image=self.mag_tk_img, tags="magnifier")
        cx, cy = mx + pw // 2, my + ph // 2
        canvas.create_line(mx, cy, mx+pw, cy, fill="black", width=3, tags="magnifier")
        canvas.create_line(mx, cy, mx+pw, cy, fill="red",   width=1, tags="magnifier")
        canvas.create_line(cx, my, cx, my+ph, fill="black", width=3, tags="magnifier")
        canvas.create_line(cx, my, cx, my+ph, fill="red",   width=1, tags="magnifier")

    @staticmethod
    def _rgb_to_lab(rgb_array):
        """RGB配列 (..., 3) uint8 → CIE L*a*b* 配列 (..., 3) float32
        RGB空間より知覚的に均一なため、色距離(ΔE)計算に使用する。
        """
        rgb = rgb_array.astype(np.float32) / 255.0
        # sRGB ガンマ除去（リニア化）
        rgb = np.where(rgb > 0.04045,
                       ((rgb + 0.055) / 1.055) ** 2.4,
                       rgb / 12.92)
        # リニアRGB → XYZ (D65 光源)
        M = np.array([[0.4124564, 0.3575761, 0.1804375],
                      [0.2126729, 0.7151522, 0.0721750],
                      [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
        xyz = rgb @ M.T
        # D65 白色点で正規化
        xyz /= np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)
        # XYZ → L*a*b*
        f = np.where(xyz > 0.008856,
                     xyz ** (1.0 / 3.0),
                     7.787 * xyz + 16.0 / 116.0)
        L = 116.0 * f[..., 1] - 16.0
        a = 500.0 * (f[..., 0] - f[..., 1])
        b = 200.0 * (f[..., 1] - f[..., 2])
        return np.stack([L, a, b], axis=-1)

    @staticmethod
    def _find_colorbar_center(region, is_vertical):
        """彩度(またはグレー分散)が最も高い列/行からカラーバー中心を推定。

        ラベル・目盛りを含む選択領域内で実際のカラーバー位置を特定する。
        カラーバーは彩度が高く、余白・ラベルは低いという性質を利用する。
        グレースケールバーの場合は輝度の行/列方向分散にフォールバック。

        Returns: region内の最良列(縦型)または最良行(横型)のindex
        """
        r = region[..., 0].astype(np.float32)
        g = region[..., 1].astype(np.float32)
        b = region[..., 2].astype(np.float32)
        cmax = np.maximum(np.maximum(r, g), b)
        cmin = np.minimum(np.minimum(r, g), b)
        sat  = np.where(cmax > 0, (cmax - cmin) / cmax, 0.0)

        axis  = 0 if is_vertical else 1
        score = sat.mean(axis=axis)          # 列(縦型)または行(横型)ごとの平均彩度

        # グレースケールのカラーバーは彩度≈0 → 輝度の分散にフォールバック
        if score.max() < 0.08:
            gray  = 0.299 * r + 0.587 * g + 0.114 * b
            score = gray.var(axis=axis)

        # 移動平均で平滑化（ノイズ除去）
        n = len(score)
        k = max(3, n // 15)
        score = np.convolve(score, np.ones(k) / k, mode='same')

        # スコアが最大値の50%以上の連続領域の中心を返す
        high = np.where(score > score.max() * 0.5)[0]
        if len(high) > 0:
            return (int(high[0]) + int(high[-1])) // 2
        return n // 2   # フォールバック: 幾何学的中心

    def show_message(self, title, hint, step):
        ACCENT  = "#3b82f6" if step == 1 else "#10b981"
        FONT_T  = ("Segoe UI", 13, "bold")
        FONT_H  = ("Segoe UI", 11)
        BX, BY  = 30, 30
        PAD_X, PAD_Y, AW, LINE_SP = 18, 12, 5, 4

        show_refresh = (step == 2 and self._calib_region is not None)
        show_close   = True   # step 1・step 2 どちらも表示
        show_quit    = True   # 常に表示（Python プロセスを完全終了）
        REFRESH_W, REFRESH_GAP = 28, 8
        CLOSE_W,   CLOSE_GAP   = 28, 4
        QUIT_W,    QUIT_GAP    = 28, 4

        ft   = tkfont.Font(font=FONT_T)
        fh   = tkfont.Font(font=FONT_H)
        tw   = max(ft.measure(title), fh.measure(hint))
        lh_t = ft.metrics("linespace")
        lh_h = fh.metrics("linespace")
        btn_extra = ((QUIT_GAP    + QUIT_W)    if show_quit    else 0) + \
                    ((CLOSE_GAP   + CLOSE_W)   if show_close   else 0) + \
                    ((REFRESH_GAP + REFRESH_W) if show_refresh else 0)
        cw   = AW + PAD_X * 2 + tw + btn_extra
        ch   = PAD_Y * 2 + lh_t + LINE_SP + lh_h

        for _, cv, _, _, _ in self.overlays:
            cv.delete("message")
            cv.delete("result")
            # ドロップシャドウ
            cv.create_rectangle(BX+3, BY+3, BX+cw+3, BY+ch+3,
                                fill="#000000", outline="", tags="message")
            # カード㋌
            cv.create_rectangle(BX, BY, BX+cw, BY+ch,
                                fill="#111827", outline="#374151", width=1, tags="message")
            # 左アクセントバー
            cv.create_rectangle(BX, BY, BX+AW, BY+ch,
                                fill=ACCENT, outline="", tags="message")
            tx = BX + AW + PAD_X
            cv.create_text(tx, BY + PAD_Y + lh_t // 2,
                           text=title, fill="#f9fafb", anchor=tk.W, font=FONT_T, tags="message")
            cv.create_text(tx, BY + PAD_Y + lh_t + LINE_SP + lh_h // 2,
                           text=hint,  fill="#9ca3af", anchor=tk.W, font=FONT_H, tags="message")
            if show_quit or show_close or show_refresh:
                btn_pad = 5
                bx_r = BX + cw - btn_pad   # 右端から左に並べる
                if show_quit:
                    bx_r -= QUIT_W
                    self._add_quit_button(cv, bx_r, BY + btn_pad,
                                          QUIT_W, ch - btn_pad * 2)
                    bx_r -= QUIT_GAP
                if show_close:
                    bx_r -= CLOSE_W
                    self._add_close_button(cv, bx_r, BY + btn_pad,
                                           CLOSE_W, ch - btn_pad * 2)
                    bx_r -= CLOSE_GAP
                if show_refresh:
                    bx_r -= REFRESH_W
                    self._add_refresh_button(cv, bx_r, BY + btn_pad,
                                             REFRESH_W, ch - btn_pad * 2)

    def _add_refresh_button(self, cv, x, y, w, h):
        """目盛り再検出ボタンをキャンバスに描画する"""
        bg_id = cv.create_rectangle(x, y, x + w, y + h,
                                     fill="#1f2937", outline="#4b5563", width=1,
                                     tags="message")
        ic_id = cv.create_text(x + w // 2, y + h // 2,
                                text="↺", fill="#9ca3af", font=("Segoe UI", 12),
                                tags="message")

        def on_click(event):
            if self._calib_region is None or self.mode != "pick_color":
                return
            self.value_map = None
            if self._colorbar_canvas:
                self._colorbar_canvas.delete("tick_label")
                self._colorbar_canvas.delete("tick_editor")
            region = self._calib_region
            self.show_message("Pick Color",
                              "Re-analyzing ticks…  •  Click to pick  •  Esc to close", 2)
            threading.Thread(target=self._calibrate_background,
                             args=(region,), daemon=True).start()

        def on_enter(event):
            cv.itemconfig(bg_id, fill="#374151")
            cv.itemconfig(ic_id, fill="#f9fafb")
            cv.config(cursor="hand2")

        def on_leave(event):
            cv.itemconfig(bg_id, fill="#1f2937")
            cv.itemconfig(ic_id, fill="#9ca3af")
            cv.config(cursor="crosshair")

        for item_id in (bg_id, ic_id):
            cv.tag_bind(item_id, "<Button-1>", on_click)
            cv.tag_bind(item_id, "<Enter>",    on_enter)
            cv.tag_bind(item_id, "<Leave>",    on_leave)

    def _add_quit_button(self, cv, x, y, w, h):
        """アプリ終了ボタンを描画する（クリックで Python プロセスを完全終了）"""
        bg_id = cv.create_rectangle(x, y, x + w, y + h,
                                     fill="#1f2937", outline="#4b5563", width=1,
                                     tags="message")
        ic_id = cv.create_text(x + w // 2, y + h // 2,
                                text="⏻", fill="#9ca3af",
                                font=("Segoe UI Symbol", 11),
                                tags="message")

        def on_click(event):
            self.root.destroy()

        def on_enter(event):
            cv.itemconfig(bg_id, fill="#78350f")
            cv.itemconfig(ic_id, fill="#fbbf24")
            cv.config(cursor="hand2")

        def on_leave(event):
            cv.itemconfig(bg_id, fill="#1f2937")
            cv.itemconfig(ic_id, fill="#9ca3af")
            cv.config(cursor="crosshair")

        for item_id in (bg_id, ic_id):
            cv.tag_bind(item_id, "<Button-1>", on_click)
            cv.tag_bind(item_id, "<Enter>",    on_enter)
            cv.tag_bind(item_id, "<Leave>",    on_leave)

    def _add_close_button(self, cv, x, y, w, h):
        """終了ボタンをキャンバスに描画する（クリックでオーバーレイを閉じる）"""
        bg_id = cv.create_rectangle(x, y, x + w, y + h,
                                     fill="#1f2937", outline="#4b5563", width=1,
                                     tags="message")
        ic_id = cv.create_text(x + w // 2, y + h // 2,
                                text="✕", fill="#9ca3af", font=("Segoe UI", 11),
                                tags="message")

        def on_click(event):
            self.close_overlay()

        def on_enter(event):
            cv.itemconfig(bg_id, fill="#7f1d1d")
            cv.itemconfig(ic_id, fill="#fca5a5")
            cv.config(cursor="hand2")

        def on_leave(event):
            cv.itemconfig(bg_id, fill="#1f2937")
            cv.itemconfig(ic_id, fill="#9ca3af")
            cv.config(cursor="crosshair")

        for item_id in (bg_id, ic_id):
            cv.tag_bind(item_id, "<Button-1>", on_click)
            cv.tag_bind(item_id, "<Enter>",    on_enter)
            cv.tag_bind(item_id, "<Leave>",    on_leave)

    def on_mouse_down(self, event):
        if self.mode == "select_bar":
            self.start_x = event.x
            self.start_y = event.y
            if self.rect_id:
                self.canvas.delete(self.rect_id)
            self.rect_id = self.canvas.create_rectangle(
                self.start_x, self.start_y, self.start_x, self.start_y,
                outline="red", width=2, dash=(4, 4)
            )
        elif self.mode == "pick_color":
            # result/message/tick_label タグ項目の上のクリックは色取得をスキップ
            hit = set(event.widget.find_overlapping(
                event.x - 1, event.y - 1, event.x + 1, event.y + 1))
            tagged = (set(event.widget.find_withtag("result")) |
                      set(event.widget.find_withtag("message")) |
                      set(event.widget.find_withtag("tick_label")) |
                      set(event.widget.find_withtag("tick_editor")))
            if hit & tagged:
                return
            if event.state & 0x1:  # Shiftキー: 目盛りを手動追加
                self._add_tick_at(event.x, event.y)
            else:
                self.pick_and_highlight(event.x, event.y)

    def on_mouse_drag(self, event):
        if self.mode == "select_bar" and self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_mouse_up(self, event):
        if self.mode == "select_bar":
            x1, y1 = min(self.start_x, event.x), min(self.start_y, event.y)
            x2, y2 = max(self.start_x, event.x), max(self.start_y, event.y)
            
            # 領域が小さすぎる場合はキャンセル扱い
            if x2 - x1 < 5 or y2 - y1 < 5:
                return

            self.colorbar_bbox = (x1, y1, x2, y2)
            self._colorbar_canvas = self.canvas
            
            # 選択領域全体を先に切り出す（OCRスレッドと共用）
            vox, voy = self.canvas_offset
            region = self.image_array[y1+voy:y2+voy, x1+vox:x2+vox].copy()

            # 彩度ベースでカラーバーの中心列/行を自動検出
            # （ラベル・目盛り込みで選択しても正確な帯を抽出できる）
            is_vertical = (y2 - y1) > (x2 - x1)
            cb_center   = self._find_colorbar_center(region, is_vertical)

            if is_vertical:
                # 縦型: 検出した中心列の前後10%幅の帯を平均
                cx   = x1 + cb_center          # region内index → canvas座標
                half = max(2, (x2 - x1) // 10)
                strip = self.image_array[y1+voy:y2+voy,
                                         cx+vox-half : cx+vox+half+1]
                self.colorbar_array = np.round(strip.mean(axis=1)).astype(np.uint8)
                self.is_vertical = True
            else:
                # 横型: 検出した中心行の前後10%幅の帯を平均
                cy   = y1 + cb_center          # region内index → canvas座標
                half = max(2, (y2 - y1) // 10)
                strip = self.image_array[cy+voy-half : cy+voy+half+1,
                                         x1+vox:x2+vox]
                self.colorbar_array = np.round(strip.mean(axis=0)).astype(np.uint8)
                self.is_vertical = False
            self.colorbar_lab = self._rgb_to_lab(self.colorbar_array)

            # フル選択領域をバックグラウンドで目盛り検出 + OCR
            self.value_map     = None
            self._calib_region = region   # 再検出ボタン用に保持
            threading.Thread(target=self._calibrate_background,
                             args=(region,), daemon=True).start()

            self.canvas.itemconfig(self.rect_id, outline="lime", dash=())
            self.mode = "pick_color"
            self.show_message("Pick Color", "Analyzing ticks…  •  Click to pick  •  Esc to close", 2)

    _OUTLIER_THRESHOLD = 15.0  # ΔE > この値で外れ値と判定

    def pick_and_highlight(self, x, y):
        ox, oy = self.canvas_offset
        target_rgb = self.image_array[y + oy, x + ox]
        target_lab = self._rgb_to_lab(target_rgb.reshape(1, 3))[0]

        distances      = np.linalg.norm(self.colorbar_lab - target_lab, axis=1)
        best_match_idx = int(np.argmin(distances))
        min_delta_e    = float(distances[best_match_idx])
        match_rgb      = self.colorbar_array[best_match_idx]

        n          = len(self.colorbar_array)
        position   = best_match_idx / max(n - 1, 1)   # 0.0(先頭) ～ 1.0(末尾)
        is_outlier = min_delta_e > self._OUTLIER_THRESHOLD

        self.draw_highlight(best_match_idx, match_rgb, position, min_delta_e, is_outlier, x, y)

    def draw_highlight(self, idx, bg_rgb, position, delta_e, is_outlier, cx, cy):
        self.canvas.delete("highlight")

        if not is_outlier:
            luminance     = 0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]
            line_color    = "white" if luminance < 128 else "black"
            outline_color = "black" if line_color == "white" else "white"
            x1, y1, x2, y2 = self.colorbar_bbox
            if self.is_vertical:
                y_pos = y1 + idx
                self.canvas.create_line(x1, y_pos, x2, y_pos, fill=outline_color, width=5, tags="highlight")
                self.canvas.create_line(x1, y_pos, x2, y_pos, fill=line_color,    width=1, tags="highlight")
            else:
                x_pos = x1 + idx
                self.canvas.create_line(x_pos, y1, x_pos, y2, fill=outline_color, width=5, tags="highlight")
                self.canvas.create_line(x_pos, y1, x_pos, y2, fill=line_color,    width=1, tags="highlight")

        if is_outlier:
            for _, cv, _, _, _ in self.overlays:
                cv.delete("result")
            self._show_outlier_toast(cx, cy, delta_e)
        else:
            self._show_result(position, delta_e)

    def _show_result(self, position, delta_e):
        FONT_T = ("Segoe UI", 13, "bold")
        FONT_H = ("Segoe UI", 11)
        BX, BY  = 30, 105
        PAD_X, PAD_Y, AW, LINE_SP = 18, 12, 5, 4

        if self.value_map and len(self.value_map) >= 2:
            # frac 昇順にソート
            sorted_map = sorted(self.value_map, key=lambda x: x[0])
            fracs = [p for p, v in sorted_map]
            vals  = [v for p, v in sorted_map]
            if position <= fracs[0]:
                # 下端より外側: 最初の2点の傾きで線形外挿
                slope = (vals[1] - vals[0]) / (fracs[1] - fracs[0])
                value = vals[0] + slope * (position - fracs[0])
            elif position >= fracs[-1]:
                # 上端より外側: 最後の2点の傾きで線形外挿
                slope = (vals[-1] - vals[-2]) / (fracs[-1] - fracs[-2])
                value = vals[-1] + slope * (position - fracs[-1])
            else:
                value = float(np.interp(position, fracs, vals))
            title     = f"Value: {value:.4g}"
            hint      = f"Position: {position:.1%}  •  \u0394E = {delta_e:.1f}"
            copy_text = f"{value:.4g}"
        else:
            quality   = "Good" if delta_e < 5 else "Fair" if delta_e < 10 else "Poor"
            title     = f"Position: {position:.1%}"
            hint      = f"\u0394E = {delta_e:.1f}  •  {quality}"
            copy_text = f"{position:.1%}"

        ft   = tkfont.Font(font=FONT_T)
        fh   = tkfont.Font(font=FONT_H)
        tw   = max(ft.measure(title), fh.measure(hint))
        lh_t = ft.metrics("linespace")
        lh_h = fh.metrics("linespace")
        COPY_W, COPY_GAP = 28, 8   # コピーボタンの幅とテキスト領域との間隔
        cw   = AW + PAD_X * 2 + tw + COPY_GAP + COPY_W
        ch   = PAD_Y * 2 + lh_t + LINE_SP + lh_h

        for _, cv, _, _, _ in self.overlays:
            cv.delete("result")
            cv.create_rectangle(BX+3, BY+3, BX+cw+3, BY+ch+3,
                                fill="#000000", outline="", tags="result")
            cv.create_rectangle(BX, BY, BX+cw, BY+ch,
                                fill="#111827", outline="#374151", width=1, tags="result")
            cv.create_rectangle(BX, BY, BX+AW, BY+ch,
                                fill="#8b5cf6", outline="", tags="result")
            tx = BX + AW + PAD_X
            cv.create_text(tx, BY + PAD_Y + lh_t // 2,
                           text=title, fill="#f9fafb", anchor=tk.W, font=FONT_T, tags="result")
            cv.create_text(tx, BY + PAD_Y + lh_t + LINE_SP + lh_h // 2,
                           text=hint,  fill="#9ca3af", anchor=tk.W, font=FONT_H, tags="result")
            # コピーボタン（カード右端に配置）
            btn_pad = 5
            self._add_copy_button(cv,
                                  BX + cw - COPY_W - btn_pad,
                                  BY + btn_pad,
                                  COPY_W, ch - btn_pad * 2,
                                  copy_text)

    def _add_copy_button(self, cv, x, y, w, h, value):
        """キャンバスにコピーアイコンボタンを描画しクリックで clipboard にコピーする"""
        bg_id = cv.create_rectangle(x, y, x + w, y + h,
                                     fill="#1f2937", outline="#4b5563", width=1,
                                     tags="result")
        ic_id = cv.create_text(x + w // 2, y + h // 2,
                                text="⧉", fill="#9ca3af", font=("Segoe UI", 11),
                                tags="result")

        def on_click(event):
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            # 成功フィードバック: 一瞬緑に光る
            cv.itemconfig(bg_id, fill="#065f46")
            cv.itemconfig(ic_id, fill="#34d399")
            cv.after(500, lambda: (cv.itemconfig(bg_id, fill="#1f2937"),
                                   cv.itemconfig(ic_id, fill="#9ca3af")))

        def on_enter(event):
            cv.itemconfig(bg_id, fill="#374151")
            cv.itemconfig(ic_id, fill="#f9fafb")
            cv.config(cursor="hand2")

        def on_leave(event):
            cv.itemconfig(bg_id, fill="#1f2937")
            cv.itemconfig(ic_id, fill="#9ca3af")
            cv.config(cursor="crosshair")

        for item_id in (bg_id, ic_id):
            cv.tag_bind(item_id, "<Button-1>", on_click)
            cv.tag_bind(item_id, "<Enter>",    on_enter)
            cv.tag_bind(item_id, "<Leave>",    on_leave)

    # -------- 目盛りラベル表示・編集 --------

    def _draw_tick_labels(self):
        """キャリブレーション済み目盛り値をカラーバー隣に描画。クリックで値を編集できる。
        縦型カラーバー: カラーバー右横に表示
        横型カラーバー: カラーバー下に表示
        """
        cv = self._colorbar_canvas
        if cv is None:
            return
        cv.delete("tick_label")
        if not self.value_map or not self.colorbar_bbox:
            return

        x1, y1, x2, y2 = self.colorbar_bbox
        FONT = ("Segoe UI", 9)
        PAD = 3

        for i, (frac, val) in enumerate(self.value_map):
            if self.is_vertical:
                tx = x2 + 8
                ty = int(y1 + frac * (y2 - y1))
                anchor = tk.W
            else:
                tx = int(x1 + frac * (x2 - x1))
                ty = y2 + 8
                anchor = tk.N

            label = f"{val:.4g}"
            tid = cv.create_text(tx, ty, text=label, fill="#e5e7eb",
                                 font=FONT, anchor=anchor, tags="tick_label")
            bb = cv.bbox(tid)
            bg_id = None
            del_id = None
            if bb:
                bg_id = cv.create_rectangle(
                    bb[0] - PAD, bb[1] - PAD, bb[2] + PAD, bb[3] + PAD,
                    fill="#1f2937", outline="#4b5563", width=1, tags="tick_label"
                )
                cv.tag_lower(bg_id, tid)
                # × 削除ボタン (ラベル背景の右隣に配置)
                del_x = bb[2] + PAD + 5
                del_y = (bb[1] + bb[3]) // 2
                del_id = cv.create_text(del_x, del_y, text="×",
                                        fill="#6b7280", font=("Segoe UI", 10),
                                        anchor=tk.W, tags="tick_label")

            def _make_handlers(idx_, frac_, tx_, ty_, anc_, bg_, t_, del_):
                def on_clk(event):
                    self._open_tick_editor(idx_, frac_, tx_, ty_, anc_)
                def on_ent(event):
                    if bg_: cv.itemconfig(bg_, fill="#374151")
                    cv.itemconfig(t_, fill="#f9fafb")
                    cv.config(cursor="hand2")
                def on_lv(event):
                    if bg_: cv.itemconfig(bg_, fill="#1f2937")
                    cv.itemconfig(t_, fill="#e5e7eb")
                    cv.config(cursor="crosshair")
                def on_del_clk(event):
                    if self.value_map and 0 <= idx_ < len(self.value_map):
                        del self.value_map[idx_]
                        cv.delete("tick_editor")
                        self._draw_tick_labels()
                def on_del_ent(event):
                    if del_: cv.itemconfig(del_, fill="#ef4444")
                    cv.config(cursor="hand2")
                def on_del_lv(event):
                    if del_: cv.itemconfig(del_, fill="#6b7280")
                    cv.config(cursor="crosshair")
                return on_clk, on_ent, on_lv, on_del_clk, on_del_ent, on_del_lv

            (on_clk, on_ent, on_lv,
             on_del_clk, on_del_ent, on_del_lv) = _make_handlers(
                i, frac, tx, ty, anchor, bg_id, tid, del_id)
            items = [tid] + ([bg_id] if bg_id else [])
            for item in items:
                cv.tag_bind(item, "<Button-1>", on_clk)
                cv.tag_bind(item, "<Enter>",    on_ent)
                cv.tag_bind(item, "<Leave>",    on_lv)
            if del_id:
                cv.tag_bind(del_id, "<Button-1>", on_del_clk)
                cv.tag_bind(del_id, "<Enter>",    on_del_ent)
                cv.tag_bind(del_id, "<Leave>",    on_del_lv)

    def _open_tick_editor(self, idx, frac, lx, ly, anchor):
        """指定インデックスの目盛り値を編集するインライン Entry をキャンバス上に表示する。
        Enter/Return: 確定して value_map を更新し再描画
        Escape: キャンセル
        フォーカスアウト: 確定
        """
        cv = self._colorbar_canvas
        if cv is None:
            return
        cv.delete("tick_editor")

        current_val = f"{self.value_map[idx][1]:.4g}"
        var = tk.StringVar(value=current_val)
        entry = tk.Entry(
            cv, textvariable=var, width=8,
            font=("Segoe UI", 9),
            bg="#111827", fg="#f9fafb",
            insertbackground="#f9fafb",
            highlightbackground="#6366f1",
            highlightcolor="#6366f1",
            highlightthickness=2,
            relief="flat", bd=0
        )
        cv.create_window(lx, ly, window=entry, anchor=anchor, tags="tick_editor")
        entry.focus_set()
        entry.select_range(0, tk.END)

        committed = [False]  # FocusOut と Return の二重発火防止

        def commit(event=None):
            if committed[0]:
                return
            committed[0] = True
            try:
                new_val = float(var.get().strip())
                f, _ = self.value_map[idx]
                self.value_map[idx] = (f, new_val)
            except ValueError:
                pass
            cv.delete("tick_editor")
            self._draw_tick_labels()

        def cancel(event=None):
            if committed[0]:
                return
            committed[0] = True
            cv.delete("tick_editor")
            self._draw_tick_labels()

        entry.bind("<Return>",   commit)
        entry.bind("<KP_Enter>", commit)
        entry.bind("<Escape>",   cancel)
        entry.bind("<FocusOut>", commit)

    def _add_tick_at(self, cx, cy):
        """カラーバー上の指定位置に目盛りを手動追加するインライン Entry を表示する。
        値を入力して Enter で追加、Escape でキャンセル。
        """
        if not self.colorbar_bbox or self._colorbar_canvas is None:
            return
        x1, y1, x2, y2 = self.colorbar_bbox
        if self.is_vertical:
            frac = (cy - y1) / max(y2 - y1, 1)
            lx, ly, anchor = x2 + 8, cy, tk.W
        else:
            frac = (cx - x1) / max(x2 - x1, 1)
            lx, ly, anchor = cx, y2 + 8, tk.N
        frac = max(0.0, min(1.0, frac))

        cv = self._colorbar_canvas
        cv.delete("tick_editor")

        var = tk.StringVar(value="")
        entry = tk.Entry(
            cv, textvariable=var, width=8,
            font=("Segoe UI", 9),
            bg="#111827", fg="#f9fafb",
            insertbackground="#f9fafb",
            highlightbackground="#10b981",
            highlightcolor="#10b981",
            highlightthickness=2,
            relief="flat", bd=0
        )
        cv.create_window(lx, ly, window=entry, anchor=anchor, tags="tick_editor")
        entry.focus_set()

        committed = [False]

        def commit(event=None):
            if committed[0]:
                return
            committed[0] = True
            try:
                new_val = float(var.get().strip())
                if self.value_map is None:
                    self.value_map = []
                self.value_map.append((frac, new_val))
                self.value_map.sort(key=lambda x: x[0])
            except ValueError:
                pass
            cv.delete("tick_editor")
            self._draw_tick_labels()

        def cancel(event=None):
            if committed[0]:
                return
            committed[0] = True
            cv.delete("tick_editor")

        entry.bind("<Return>",   commit)
        entry.bind("<KP_Enter>", commit)
        entry.bind("<Escape>",   cancel)
        entry.bind("<FocusOut>", commit)

    def _calibrate_background(self, region):
        """バックグラウンドスレッド: 目盛り検出 → OCR → value_map 構築"""
        try:
            pairs = self._detect_ticks_and_ocr(region, self.is_vertical)
            if len(pairs) >= 2:
                pairs.sort(key=lambda x: x[0])
                self.root.after(0, lambda: self._on_calibration_done(pairs))
            else:
                print(f"Calibration: only {len(pairs)} tick(s) found (need ≥ 2)")
                self.root.after(0, self._on_calibration_failed)
        except Exception as e:
            import traceback
            print(f"Calibration error: {e}")
            traceback.print_exc()
            self.root.after(0, self._on_calibration_failed)

    def _on_calibration_done(self, pairs):
        self.value_map = pairs
        vmin = min(v for _, v in pairs)
        vmax = max(v for _, v in pairs)
        self.show_message("Pick Color",
                          f"{len(pairs)} ticks  [{vmin:.4g} – {vmax:.4g}]  •  Shift+click: +tick  •  Esc to close", 2)
        self._draw_tick_labels()

    def _on_calibration_failed(self):
        if self._colorbar_canvas:
            self._colorbar_canvas.delete("tick_label")
            self._colorbar_canvas.delete("tick_editor")
        self.show_message("Pick Color",
                          "No ticks found (showing position%)  •  Click to pick  •  Esc to close", 2)

    def _detect_ticks_and_ocr(self, region, is_vertical):
        """目盛り位置を検出してOCRで値を読み取る。[(frac, value), ...] を返す。"""
        cb_center = self._find_colorbar_center(region, is_vertical)
        try:
            reader = self._ensure_ocr_reader()
        except ImportError:
            print("easyocr not installed: pip install easyocr")
            return []

        tick_fracs, label_side = self._find_tick_positions(region, is_vertical, cb_center)
        if len(tick_fracs) < 2:
            # 三角終端・断続目盛りなどでtick線検出が弱い場合は、
            # ラベル位置のみでキャリブレーションするフォールバック。
            pairs = self._ocr_pairs_from_labels_only(region, is_vertical, reader, cb_center)
            if len(pairs) >= 2:
                return pairs
            # 緩い再試行
            return self._ocr_pairs_from_labels_only(region, is_vertical, reader, cb_center, relax=True)

        H, W    = region.shape[:2]
        total   = (H if is_vertical else W) - 1
        tick_px = [int(f * total) for f in tick_fracs]

        sides = [label_side, self._opposite_side(label_side)]
        best_pairs = []
        best_score = -1.0

        # まずラベル帯を一括OCRし、最近傍の目盛りに割り当てる。
        # 1 tickごとのOCRより、値と位置の対応ズレが起きにくい。
        for tol_scale in (1.0, 1.45):
            for side in sides:
                pairs = self._ocr_pairs_by_tick_alignment(
                    region, tick_px, is_vertical, side, reader, cb_center, tol_scale=tol_scale
                )
                filtered = self._filter_value_pairs(pairs)
                score = self._pair_quality_score(filtered, expected_n=len(tick_px))
                if score > best_score:
                    best_pairs = filtered
                    best_score = score

        if len(best_pairs) >= 2:
            return best_pairs

        # フォールバック: 従来の1 tickずつOCR
        fallback_best = []
        fallback_score = -1.0
        for lh_scale in (1.0, 1.5):
            for side in sides:
                pairs = []
                for i, (frac, px) in enumerate(zip(tick_fracs, tick_px)):
                    gaps = []
                    if i > 0:
                        gaps.append(px - tick_px[i - 1])
                    if i < len(tick_px) - 1:
                        gaps.append(tick_px[i + 1] - px)
                    max_lh = max(8, min(64, int((min(gaps) // 2) * lh_scale))) if gaps else int(48 * lh_scale)

                    val = self._ocr_label_at(
                        region, frac, is_vertical, side, reader, max_lh, cb_center)
                    if val is not None:
                        pairs.append((frac, val))
                filtered = self._filter_value_pairs(pairs)
                score = self._pair_quality_score(filtered, expected_n=len(tick_px))
                if score > fallback_score:
                    fallback_best = filtered
                    fallback_score = score
        return fallback_best

    @staticmethod
    def _opposite_side(side):
        table = {
            'right': 'left',
            'left': 'right',
            'top': 'bottom',
            'bottom': 'top',
        }
        return table.get(side, side)

    @staticmethod
    def _pair_quality_score(pairs, expected_n=0):
        """(frac, value) 列の妥当性スコア。高いほど良い。"""
        n = len(pairs)
        if n < 2:
            return -1.0
        pairs = sorted(pairs, key=lambda x: x[0])
        fracs = np.array([f for f, _ in pairs], dtype=np.float64)
        vals = np.array([v for _, v in pairs], dtype=np.float64)
        # 単調性
        d = np.diff(vals)
        mono = 1.0
        if len(d) > 0:
            direction = 1.0 if np.median(d) >= 0 else -1.0
            mono = float(np.mean(direction * d >= 0))
        # 線形当てはまり
        if len(fracs) >= 2:
            p = np.polyfit(fracs, vals, 1)
            pred = p[0] * fracs + p[1]
            rmse = float(np.sqrt(np.mean((vals - pred) ** 2)))
            val_range = float(np.max(vals) - np.min(vals))
            fit = 1.0 / (1.0 + rmse / max(val_range, 1e-6))
        else:
            fit = 0.0
        cov = min(1.0, n / max(expected_n, 2)) if expected_n else min(1.0, n / 6.0)
        return 1.6 * mono + 1.2 * fit + 1.0 * cov + 0.2 * n

    @staticmethod
    def _value_candidates_from_text(text):
        """OCR文字列からあり得る数値候補を生成する。"""
        s = text.strip().replace(' ', '')
        if not s:
            return []

        vals = []

        def _add(v):
            if np.isfinite(v):
                vals.append(float(v))

        try:
            _add(float(s))
        except ValueError:
            pass

        sign = -1.0 if s.startswith('-') else 1.0
        body = s[1:] if s.startswith(('-', '+')) else s
        if body.isdigit() and body:
            # 1桁は 0.x を候補に追加
            if len(body) == 1:
                _add(sign * float(f"0.{body}"))

            # 100 -> 1.00 のような小数点脱落を補う
            if len(body) >= 3:
                _add(sign * float(f"{body[:-2]}.{body[-2:]}"))

            # 25/50/75 は 0.25/0.50/0.75 の読み落としが多い
            if body in ("25", "50", "75"):
                _add(sign * float(f"0.{body}"))

        uniq = []
        seen = set()
        for v in vals:
            key = round(v, 8)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(v)
        return uniq

    def _ocr_pairs_from_labels_only(self, region, is_vertical, reader, cb_center=None, relax=False):
        """tick線が取れない場合に、ラベル位置のみで (frac, value) を推定する。"""
        sides = ('right', 'left') if is_vertical else ('bottom', 'top')
        best_pairs = []
        best_score = -1.0
        for side in sides:
            cand = self._extract_label_candidates(region, is_vertical, side, reader, cb_center, relax=relax)
            if len(cand) < 2:
                continue
            pairs = self._filter_value_pairs([(f, v) for f, v, _ in cand])
            if len(pairs) < 2:
                continue
            score = self._pair_quality_score(pairs)
            if score > best_score:
                best_pairs = pairs
                best_score = score
        return best_pairs

    def _extract_label_candidates(self, region, is_vertical, side, reader, cb_center=None, relax=False):
        """指定sideのラベル帯から (frac, value, conf) 候補を抽出する。"""
        H, W = region.shape[:2]
        if is_vertical:
            cx = cb_center if cb_center is not None else W // 2
            half = max(2, W // 10)
            band_w = max(40, min(W // 2, 140))
            if side == 'right':
                x0 = min(W, cx + half)
                x1 = min(W, x0 + band_w)
            else:
                x1 = max(0, cx - half)
                x0 = max(0, x1 - band_w)
            y0, y1 = 0, H
        else:
            cy = cb_center if cb_center is not None else H // 2
            half = max(2, H // 10)
            band_h = max(28, min(H // 2, 90))
            if side == 'bottom':
                y0 = min(H, cy + half)
                y1 = min(H, y0 + band_h)
            else:
                y1 = max(0, cy - half)
                y0 = max(0, y1 - band_h)
            x0, x1 = 0, W

        if x1 - x0 < 6 or y1 - y0 < 6:
            return []

        crop = region[y0:y1, x0:x1]
        gray = ImageOps.autocontrast(Image.fromarray(crop).convert('L'), cutoff=2)
        variants = [np.array(gray.convert('RGB')), np.array(ImageOps.invert(gray).convert('RGB'))]
        if relax:
            bw = gray.point(lambda p: 255 if p > 168 else 0)
            variants.extend([
                np.array(bw.convert('RGB')),
                np.array(ImageOps.invert(bw).convert('RGB')),
            ])

        _SUBS = str.maketrans('OolISBZG', '00115826')
        raw = []  # (frac, value, conf)
        min_conf = 0.20 if relax else 0.28
        for img_arr in variants:
            results = reader.readtext(
                img_arr,
                detail=1,
                allowlist='0123456789.-eE+',
                adjust_contrast=0.5,
                paragraph=False,
                width_ths=0.7,
                contrast_ths=0.05,
            )
            for box, text, conf in results:
                if float(conf) < min_conf:
                    continue
                s = text.strip().replace(' ', '').translate(_SUBS)
                cands = self._value_candidates_from_text(s)
                if not cands:
                    continue
                # 文字列長と値の過大さを弱く正則化して候補を選ぶ
                def _cand_score(v):
                    score = float(conf)
                    if '.' in s:
                        score += 0.03
                    if abs(v) <= 200:
                        score += 0.02
                    if abs(v) <= 20:
                        score += 0.02
                    return score

                val = max(cands, key=_cand_score)
                if is_vertical:
                    axis_local = (box[0][1] + box[2][1]) / 2.0
                    frac = (y0 + axis_local) / max(H - 1, 1)
                else:
                    axis_local = (box[0][0] + box[2][0]) / 2.0
                    frac = (x0 + axis_local) / max(W - 1, 1)
                frac = max(0.0, min(1.0, float(frac)))
                raw.append((frac, float(val), float(conf)))

        if not raw:
            return []

        # 近接ラベルをクラスタリングし、高信頼なものだけ残す
        raw.sort(key=lambda t: t[0])
        clustered = []
        cluster = [raw[0]]
        for item in raw[1:]:
            frac_tol = 0.024 if relax else 0.018
            if abs(item[0] - cluster[-1][0]) <= frac_tol:
                cluster.append(item)
            else:
                clustered.append(max(cluster, key=lambda t: t[2]))
                cluster = [item]
        clustered.append(max(cluster, key=lambda t: t[2]))
        return clustered

    def _ocr_pairs_by_tick_alignment(self, region, tick_px, is_vertical, side, reader, cb_center=None, tol_scale=1.0):
        """ラベル帯を一括OCRし、文字中心を最近傍の目盛りに対応付ける。"""
        if not tick_px:
            return []

        H, W = region.shape[:2]
        if is_vertical:
            cx = cb_center if cb_center is not None else W // 2
            half = max(2, W // 10)
            band_w = max(40, min(W // 2, 140))
            if side == 'right':
                x0 = min(W, cx + half)
                x1 = min(W, x0 + band_w)
            else:
                x1 = max(0, cx - half)
                x0 = max(0, x1 - band_w)
            y0, y1 = 0, H
        else:
            cy = cb_center if cb_center is not None else H // 2
            half = max(2, H // 10)
            band_h = max(28, min(H // 2, 90))
            if side == 'bottom':
                y0 = min(H, cy + half)
                y1 = min(H, y0 + band_h)
            else:
                y1 = max(0, cy - half)
                y0 = max(0, y1 - band_h)
            x0, x1 = 0, W

        if x1 - x0 < 6 or y1 - y0 < 6:
            return []

        parsed = []
        for frac, val, conf in self._extract_label_candidates(
            region, is_vertical, side, reader, cb_center, relax=(tol_scale > 1.2)
        ):
            axis_px = frac * max((H if is_vertical else W) - 1, 1)
            parsed.append((axis_px, val, conf))

        if not parsed:
            return []

        # 最短目盛り間隔を基に対応許容幅を決める
        sorted_ticks = sorted(tick_px)
        gaps = [b - a for a, b in zip(sorted_ticks, sorted_ticks[1:]) if b - a > 0]
        min_gap = min(gaps) if gaps else 20
        total_len = max((H if is_vertical else W) - 1, 1)
        # 目盛り間隔 + 画像解像度に応じて自動調整
        tol_by_gap = min_gap * 0.42
        tol_by_size = total_len * 0.018
        tol = max(6.0, min(36.0, max(tol_by_gap, tol_by_size))) * float(tol_scale)

        best_for_tick = {}  # tick_idx -> (conf, value)
        for axis_px, val, conf in parsed:
            idx = min(range(len(tick_px)), key=lambda i: abs(tick_px[i] - axis_px))
            dist = abs(tick_px[idx] - axis_px)
            if dist > tol:
                continue
            prev = best_for_tick.get(idx)
            if prev is None or conf > prev[0]:
                best_for_tick[idx] = (conf, val)

        total = (H if is_vertical else W) - 1
        pairs = []
        for idx, (_, val) in best_for_tick.items():
            frac = tick_px[idx] / max(total, 1)
            pairs.append((frac, val))
        pairs.sort(key=lambda x: x[0])
        return pairs

    @staticmethod
    def _filter_value_pairs(pairs):
        """OCRの外れ値を軽く除去し、単調な目盛り列を優先する。"""
        if len(pairs) < 3:
            return pairs

        pairs = sorted(pairs, key=lambda x: x[0])
        fracs = np.array([f for f, _ in pairs], dtype=np.float64)
        vals = np.array([v for _, v in pairs], dtype=np.float64)

        # RANSAC風に最も整合する直線を選び、あり得ない外れ値を落とす
        n = len(vals)
        best_mask = np.ones(n, dtype=bool)
        best_count = 0
        best_err = float('inf')

        val_range = float(np.max(vals) - np.min(vals)) if n > 1 else 0.0
        dvals = np.abs(np.diff(np.sort(vals))) if n > 2 else np.array([0.0])
        step = float(np.median(dvals[dvals > 0])) if np.any(dvals > 0) else 1.0
        tol = max(0.08 * max(val_range, 1.0), 0.8 * max(step, 0.1), 0.12)

        for i in range(n):
            for j in range(i + 1, n):
                df = fracs[j] - fracs[i]
                if abs(df) < 1e-9:
                    continue
                m = (vals[j] - vals[i]) / df
                b = vals[i] - m * fracs[i]
                pred = m * fracs + b
                err = np.abs(vals - pred)
                mask = err <= tol
                cnt = int(mask.sum())
                med_err = float(np.median(err[mask])) if cnt > 0 else float('inf')
                if cnt > best_count or (cnt == best_count and med_err < best_err):
                    best_count = cnt
                    best_err = med_err
                    best_mask = mask

        inlier_ratio = float(np.mean(best_mask))
        filtered = [p for p, k in zip(pairs, best_mask) if bool(k)]
        # 強すぎる削除を避ける: inlierが少なすぎる場合は全候補を維持
        if len(filtered) < 2 or inlier_ratio < 0.55:
            filtered = pairs

        # 単調方向も最終チェック
        vals2 = np.array([v for _, v in filtered], dtype=np.float64)
        if len(vals2) >= 3:
            d = np.diff(vals2)
            direction = 1.0 if np.median(d) >= 0 else -1.0
            ok = direction * d >= 0
            if ok.mean() >= 0.6:
                keep = [True] + [bool(x) for x in ok]
                mono = [p for p, k in zip(filtered, keep) if k]
                if len(mono) >= 2:
                    filtered = mono

        return filtered

    def _find_tick_positions(self, region, is_vertical, cb_center=None):
        """目盛り線を検出。(tick_fractions_list, side) を返す。"""
        H, W = region.shape[:2]

        def best_in_band(b0, b1, total_len, horizontal=False):
            """バンド内を複数の細いストリップで走査し、最良の結果を返す。"""
            bw = b1 - b0
            sw = max(2, min(12, bw))
            best_ticks, best_score = [], 0.0
            step = max(1, sw // 2)
            for sx in range(b0, b1 - sw + 1, step):
                sx1 = min(b1, sx + sw)
                strip = (region[sx:sx1, :, :].transpose(1, 0, 2)
                         if horizontal else region[:, sx:sx1, :])
                t = self._ticks_from_strip(strip, total_len)
                score = self._tick_score(t, total_len)
                if score > best_score:
                    best_ticks, best_score = t, score
            # フォールバック: バンド全体を1ストリップとして試す
            if len(best_ticks) < 2:
                strip = (region[b0:b1, :, :].transpose(1, 0, 2)
                         if horizontal else region[:, b0:b1, :])
                t = self._ticks_from_strip(strip, total_len)
                if len(t) > len(best_ticks):
                    best_ticks = t
            return best_ticks, self._tick_score(best_ticks, total_len)

        best = ([], 'right', 0.0)
        if is_vertical:
            cx   = cb_center if cb_center is not None else W // 2
            half = max(2, W // 10)
            # 目盛りマーカーが延びる幅のみ走査（ラベルテキスト領域を除外する）
            tick_zone = max(8, min(self._TICK_MAX_ZONE, W // 4))
            for side, z_start, z_end in [
                ('right', cx + half,                     min(W, cx + half + tick_zone)),
                ('left',  max(0, cx - half - tick_zone), cx - half),
            ]:
                z_start, z_end = max(0, z_start), min(W, z_end)
                if z_end - z_start < 2:
                    continue
                ticks, score = best_in_band(z_start, z_end, H)
                if len(ticks) >= 2 and score > best[2]:
                    best = (ticks, side, score)
        else:
            cy   = cb_center if cb_center is not None else H // 2
            half = max(2, H // 10)
            tick_zone = max(8, min(self._TICK_MAX_ZONE, H // 4))
            for side, z_start, z_end in [
                ('bottom', cy + half,                     min(H, cy + half + tick_zone)),
                ('top',    max(0, cy - half - tick_zone), cy - half),
            ]:
                z_start, z_end = max(0, z_start), min(H, z_end)
                if z_end - z_start < 2:
                    continue
                ticks, score = best_in_band(z_start, z_end, W, horizontal=True)
                if len(ticks) >= 2 and score > best[2]:
                    best = (ticks, side, score)
        if len(best[0]) >= 2:
            total = H if is_vertical else W
            return [t / max(total - 1, 1) for t in best[0]], best[1]
        return [], 'right'

    @staticmethod
    def _tick_score(ticks, total_len):
        """目盛りリストの品質スコアを返す（多く・等間隔ほど高い）。"""
        n = len(ticks)
        if n < 2:
            return float(n)
        gaps = [ticks[i + 1] - ticks[i] for i in range(n - 1)]
        mean_g = sum(gaps) / len(gaps)
        if mean_g == 0:
            return 0.0
        variance = sum((g - mean_g) ** 2 for g in gaps) / len(gaps)
        std_g = variance ** 0.5
        return n * (1.0 / (1.0 + std_g / mean_g))

    def _ticks_from_strip(self, strip, total_len):
        """
        strip: (total_len, strip_width, 3) の細い帯。
        輝度が背景から逸脱した行の中心インデックスリストを返す。
        明背景・暗背景の両方に対応。
        """
        gray     = (0.299 * strip[..., 0] +
                    0.587 * strip[..., 1] +
                    0.114 * strip[..., 2]).astype(np.float32)
        row_mean = gray.mean(axis=1)                    # (total_len,)

        # 局所ノイズに引きずられないよう軽く平滑化
        if len(row_mean) >= 5:
            row_mean = np.convolve(row_mean, np.ones(5, dtype=np.float32) / 5.0, mode='same')

        def extract_ticks(bg_pct):
            bg  = np.percentile(row_mean, bg_pct)
            dev = np.abs(row_mean - bg)
            if dev.max() < 5:
                return []
            med = np.median(dev)
            mad = np.median(np.abs(dev - med)) + 1e-6
            thr = max(dev.max() * 0.22, med + 2.5 * mad)
            is_tick = dev > thr
            ticks, i = [], 0
            while i < total_len:
                if is_tick[i]:
                    j = i + 1
                    while j < total_len and is_tick[j]:
                        j += 1
                    ticks.append((i + j) // 2)
                    i = j
                else:
                    i += 1
            filtered = [ticks[0]] if ticks else []
            for t in ticks[1:]:
                if t - filtered[-1] >= self._TICK_MIN_GAP:
                    filtered.append(t)
            return filtered

        # 明背景(暗い目盛り)と暗背景(明るい目盛り)の両方を試して多い方を採用
        ticks_light = extract_ticks(80)
        ticks_dark  = extract_ticks(20)
        return ticks_light if len(ticks_light) >= len(ticks_dark) else ticks_dark

    def _ocr_label_at(self, region, frac, is_vertical, side, reader, lh=40, cb_center=None):
        """目盛り位置の近傍をクロップしてOCR。float を返す。失敗時は None。
        lh: OCRクロップの上下マージン(px)。隔隣目盛りとの距離の半分を渡すことで
        補助目盛りの隣の主目盛りラベルを誤読するのを防ぐ。
        """
        H, W = region.shape[:2]
        if is_vertical:
            y    = int(frac * (H - 1))
            cx   = cb_center if cb_center is not None else W // 2
            half = max(2, W // 10)
            y0, y1 = max(0, y - lh), min(H, y + lh)
            if side == 'right':
                x0, x1 = cx + half, W
            else:
                x0, x1 = 0, max(0, cx - half)
        else:
            x    = int(frac * (W - 1))
            cy   = cb_center if cb_center is not None else H // 2
            half = max(2, H // 10)
            x0, x1 = max(0, x - lh), min(W, x + lh)
            if side == 'bottom':
                y0, y1 = cy + half, H
            else:
                y0, y1 = 0, max(0, cy - half)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None

        # パディング追加（端の文字が切れるのを防ぐ）
        PAD = 6
        y0 = max(0, y0 - PAD); y1 = min(H, y1 + PAD)
        x0 = max(0, x0 - PAD); x1 = min(W, x1 + PAD)
        crop = region[y0:y1, x0:x1]

        # アップスケール（目標高さ96px、LANCZOSで高品質補間）
        ch, cw = crop.shape[:2]
        scale = max(1, 96 // max(ch, 1))
        pil_img = Image.fromarray(crop)
        if scale > 1:
            pil_img = pil_img.resize((cw * scale, ch * scale), Image.LANCZOS)

        # グレースケール変換 + 自動コントラスト強調
        gray = ImageOps.autocontrast(pil_img.convert('L'), cutoff=2)

        _SUBS = str.maketrans('OolISBZG', '00115826')

        def _ocr_best(img_arr):
            results = reader.readtext(img_arr, detail=1,
                                      allowlist='0123456789.-eE+',
                                      adjust_contrast=0.5,
                                      paragraph=False,
                                      width_ths=0.7,
                                      contrast_ths=0.05)
            if not results:
                return None, 0.0
            # 左→右の順にソート（小数点が別ボックスになる場合に備える）
            results.sort(key=lambda r: (r[0][0][0] + r[0][2][0]) / 2)
            texts = [r[1].strip().replace(' ', '').translate(_SUBS) for r in results]
            confs = [r[2] for r in results]
            n = len(results)

            candidates = []  # (val, eff_conf)

            def _try(s, eff_conf):
                try:
                    candidates.append((float(s), eff_conf))
                except ValueError:
                    pass

            # 個別ボックス
            for t, c in zip(texts, confs):
                if c >= 0.25:
                    _try(t, c)

            # 隣接ボックスをマージ（"0"+"8"→"08" or "0.8" など）
            for i in range(n):
                for j in range(i + 2, n + 1):
                    chunk = texts[i:j]
                    avg_c = sum(confs[i:j]) / (j - i)
                    if avg_c < 0.15:
                        continue
                    _try(''.join(chunk), avg_c * 0.9)          # 単純連結
                    if len(chunk) == 2:                         # 2ボックス: "."を挿入
                        _try(chunk[0] + '.' + chunk[1], avg_c * 0.95)
                    if len(chunk) == 3:                         # 3ボックス: 各位置に挿入
                        _try(chunk[0] + '.' + chunk[1] + chunk[2], avg_c * 0.90)
                        _try(chunk[0] + chunk[1] + '.' + chunk[2], avg_c * 0.90)

            if not candidates:
                return None, 0.0

            # 小数点を含む数値を優先（"0.8" > "0" or "8"）; 次に有効文字数; 次に信頼度
            def _score(vc):
                val, conf = vc
                s = repr(val)
                has_meaningful_dec = ('.' in s and not s.endswith('.0'))
                n_chars = len(s.replace('.', '').replace('-', ''))
                return (has_meaningful_dec, n_chars, conf)

            best_val, best_conf = max(candidates, key=_score)
            return best_val, best_conf

        # 複数前処理を試し、最良信頼度を採用
        crop_normal = np.array(gray.convert('RGB'))
        crop_inv = np.array(ImageOps.invert(gray).convert('RGB'))
        bw = gray.point(lambda p: 255 if p > 170 else 0)
        crop_bw = np.array(bw.convert('RGB'))
        bw_inv = ImageOps.invert(bw)
        crop_bw_inv = np.array(bw_inv.convert('RGB'))

        best_val, best_conf = None, 0.0
        for variant in (crop_normal, crop_inv, crop_bw, crop_bw_inv):
            cand_val, cand_conf = _ocr_best(variant)
            if cand_conf > best_conf:
                best_val, best_conf = cand_val, cand_conf

        return best_val

    def _ensure_ocr_reader(self):
        """EasyOCRリーダーを遅延初期化。未インストール時は ImportError を送出。"""
        if self._easyocr_reader is None:
            import easyocr                          # ImportError → 呼び出し元でキャッチ
            print("Loading EasyOCR model (first run may take a while)...")
            self._easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
            print("EasyOCR ready")
        return self._easyocr_reader

    def _show_outlier_toast(self, cx, cy, delta_e):
        """カーソル近くに外れ値トーストを表示してフェードアウトする"""
        # 既存トーストを破棄
        if self._toast_window:
            try: self._toast_window.destroy()
            except tk.TclError: pass

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes('-topmost', True)
        toast.attributes('-alpha', 1.0)
        toast.configure(bg='#111827')
        self._toast_window = toast

        # レイアウト: 左アクセントバー + テキスト
        row = tk.Frame(toast, bg='#111827', bd=1, relief='flat',
                       highlightbackground='#374151', highlightthickness=1)
        row.pack()
        tk.Frame(row, bg='#ef4444', width=5).pack(side='left', fill='y')
        inner = tk.Frame(row, bg='#111827', padx=14, pady=9)
        inner.pack(side='left')
        tk.Label(inner, text='Outlier',
                 font=('Segoe UI', 13, 'bold'), fg='#f9fafb', bg='#111827').pack(anchor='w')
        tk.Label(inner, text=f'Color not on colorbar  •  \u0394E\u2009=\u2009{delta_e:.1f}',
                 font=('Segoe UI', 11), fg='#9ca3af', bg='#111827').pack(anchor='w')

        # サイズ確定後にカーソル近くへ配置
        toast.update_idletasks()
        tw = toast.winfo_width()
        th = toast.winfo_height()
        sx = self.canvas.winfo_rootx() + cx
        sy = self.canvas.winfo_rooty() + cy
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        off = 18
        px = sx + off if sx + off + tw < self.canvas.winfo_rootx() + cw else sx - off - tw
        py = sy - th - off if sy - th - off >= self.canvas.winfo_rooty() else sy + off
        toast.geometry(f'+{px}+{py}')

        # フェードアウトアニメーション(400ms待機後、600msかけて消える)
        def fade(alpha=1.0):
            alpha = round(alpha - 0.05, 3)
            if alpha <= 0:
                try: toast.destroy()
                except tk.TclError: pass
                return
            try:
                toast.attributes('-alpha', alpha)
                self.root.after(30, lambda: fade(alpha))
            except tk.TclError:
                pass

        self.root.after(400, fade)

    def close_overlay(self, event=None):
        if self._toast_window:
            try: self._toast_window.destroy()
            except tk.TclError: pass
            self._toast_window = None
        for ov, _, _, _, _ in self.overlays:
            ov.destroy()
        self.overlays = []
        self.canvas = None
        self.canvas_offset = (0, 0)
        self.colorbar_array = None
        self.colorbar_lab   = None
        self.colorbar_bbox  = None
        self._colorbar_canvas = None
        self.value_map      = None
        self._calib_region  = None
        self.mag_tk_img = None
        self._toast_window = None
        self.mode = "wait"
        print("Back to standby.")

if __name__ == "__main__":
    app = ColorPickerApp()
    app.root.mainloop() # アプリをバックグラウンドで待機させ続ける