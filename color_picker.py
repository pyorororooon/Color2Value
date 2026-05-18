import tkinter as tk
import tkinter.font as tkfont
import numpy as np
import mss
from PIL import Image, ImageTk, ImageOps
import keyboard
import threading

class ColorPickerApp:
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

        icon_img = Image.open(r"D:\download\Color2Valueapp\Color2Value.ico")

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
        img_path = r"D:\download\Color2Valueapp\Sea Surface Salinity.jpg"
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
        cv.create_image(0, 0, anchor=tk.NW, image=tk_img)
        cv._img = tk_img  # GC防止

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
            # result/messageタグ項目（ボタン等）の上のクリックは色取得をスキップ
            hit = set(event.widget.find_overlapping(
                event.x - 1, event.y - 1, event.x + 1, event.y + 1))
            tagged = (set(event.widget.find_withtag("result")) |
                      set(event.widget.find_withtag("message")))
            if hit & tagged:
                return
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
            fracs = [p for p, v in self.value_map]
            vals  = [v for p, v in self.value_map]
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

    # -------- 目盛り自動検出 + EasyOCR キャリブレーション --------

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
                          f"{len(pairs)} ticks detected  [{vmin:.4g} – {vmax:.4g}]  •  Esc to close", 2)

    def _on_calibration_failed(self):
        self.show_message("Pick Color",
                          "No ticks found (showing position%)  •  Click to pick  •  Esc to close", 2)

    def _detect_ticks_and_ocr(self, region, is_vertical):
        """目盛り位置を検出してOCRで値を読み取る。[(frac, value), ...] を返す。"""
        tick_fracs, label_side = self._find_tick_positions(region, is_vertical)
        if len(tick_fracs) < 2:
            return []
        try:
            reader = self._ensure_ocr_reader()
        except ImportError:
            print("easyocr not installed: pip install easyocr")
            return []

        H, W    = region.shape[:2]
        total   = (H if is_vertical else W) - 1
        tick_px = [int(f * total) for f in tick_fracs]

        pairs = []
        for i, (frac, px) in enumerate(zip(tick_fracs, tick_px)):
            # 隣接目盛りとの距離の半分をOCRウィンドウ高さの上限にする。
            # 補助目盛り: 遙い → ウィンドウが素数px → OCR失敗 → 自然と除外
            # 主目盛り: 広い → ウィンドウが十分 → 正常読み取り
            gaps = []
            if i > 0:
                gaps.append(px - tick_px[i - 1])
            if i < len(tick_px) - 1:
                gaps.append(tick_px[i + 1] - px)
            max_lh = max(4, min(18, min(gaps) // 2 - 1)) if gaps else 18

            val = self._ocr_label_at(
                region, frac, is_vertical, label_side, reader, max_lh)
            if val is not None:
                pairs.append((frac, val))
        return pairs

    def _find_tick_positions(self, region, is_vertical):
        """目盛り線を検出。(tick_fractions_list, side) を返す。"""
        H, W = region.shape[:2]
        if is_vertical:
            cx, half = W // 2, max(2, W // 10)
            sw = min(8, max(3, W // 15))   # 目盛り帯幅
            for side, x0, x1 in [
                ('right', cx + half,       cx + half + sw),
                ('left',  cx - half - sw,  cx - half),
            ]:
                x0, x1 = max(0, x0), min(W, x1)
                if x1 - x0 < 2:
                    continue
                ticks = self._ticks_from_strip(region[:, x0:x1, :], H)
                if len(ticks) >= 2:
                    return [t / max(H - 1, 1) for t in ticks], side
        else:
            cy, half = H // 2, max(2, H // 10)
            sw = min(8, max(3, H // 15))
            for side, y0, y1 in [
                ('bottom', cy + half,       cy + half + sw),
                ('top',    cy - half - sw,  cy - half),
            ]:
                y0, y1 = max(0, y0), min(H, y1)
                if y1 - y0 < 2:
                    continue
                ticks = self._ticks_from_strip(
                    region[y0:y1, :, :].transpose(1, 0, 2), W)
                if len(ticks) >= 2:
                    return [t / max(W - 1, 1) for t in ticks], side
        return [], 'right'

    def _ticks_from_strip(self, strip, total_len):
        """
        strip: (total_len, strip_width, 3) の細い帯。
        輝度が背景から逸脱した行の中心インデックスリストを返す。
        """
        gray     = (0.299 * strip[..., 0] +
                    0.587 * strip[..., 1] +
                    0.114 * strip[..., 2]).astype(np.float32)
        row_mean = gray.mean(axis=1)                    # (total_len,)
        bg       = np.percentile(row_mean, 80)
        dev      = np.abs(row_mean - bg)
        if dev.max() < 10:
            return []
        is_tick = dev > dev.max() * 0.35
        # 連続するTrue行をクラスタリングして中心を取得
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
        # 近接目盛り(<5px)を除去
        filtered = [ticks[0]] if ticks else []
        for t in ticks[1:]:
            if t - filtered[-1] >= 5:
                filtered.append(t)
        return filtered

    def _ocr_label_at(self, region, frac, is_vertical, side, reader, lh=18):
        """目盛り位置の近傍をクロップしてOCR。float を返す。失敗時は None。
        lh: OCRクロップの上下マージン(px)。隔隣目盛りとの距離の半分を渡すことで
        補助目盛りの隣の主目盛りラベルを誤読するのを防ぐ。
        """
        H, W = region.shape[:2]
        if is_vertical:
            y    = int(frac * (H - 1))
            cx, half = W // 2, max(2, W // 10)
            sw   = min(8, max(3, W // 15))
            y0, y1 = max(0, y - lh), min(H, y + lh)
            if side == 'right':
                x0, x1 = cx + half + sw, W
            else:
                x0, x1 = 0, max(0, cx - half - sw)
        else:
            x    = int(frac * (W - 1))
            cy, half = H // 2, max(2, H // 10)
            sw   = min(8, max(3, H // 15))
            x0, x1 = max(0, x - lh), min(W, x + lh)
            if side == 'bottom':
                y0, y1 = cy + half + sw, H
            else:
                y0, y1 = 0, max(0, cy - half - sw)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None

        # パディング追加（端の文字が切れるのを防ぐ）
        PAD = 8
        y0 = max(0, y0 - PAD); y1 = min(H, y1 + PAD)
        x0 = max(0, x0 - PAD); x1 = min(W, x1 + PAD)
        crop = region[y0:y1, x0:x1]

        # アップスケール（目標高さ80px、LANCZOSで高品質補間）
        # NEAREST は数字の輪郭が階段状になり誤認識の原因になる
        ch, cw = crop.shape[:2]
        scale = max(1, 80 // max(ch, 1))
        pil_img = Image.fromarray(crop)
        if scale > 1:
            pil_img = pil_img.resize((cw * scale, ch * scale), Image.LANCZOS)

        # グレースケール変換 + 自動コントラスト強調
        # 上下2%を飽和させてコントラストを最大化（薄いテキストも明確に）
        gray = ImageOps.autocontrast(pil_img.convert('L'), cutoff=2)
        crop_final = np.array(gray.convert('RGB'))

        # OCR: detail=1 で信頼度スコアを取得し、最高信頼度の結果を採用
        results = reader.readtext(crop_final, detail=1,
                                  allowlist='0123456789.-',
                                  adjust_contrast=0.5)
        best_val, best_conf = None, 0.0
        for (_, text, conf) in results:
            if conf < 0.35:
                continue
            text = text.strip().replace(' ', '').replace('O', '0').replace('l', '1')
            try:
                val = float(text)
                if conf > best_conf:
                    best_val, best_conf = val, conf
            except ValueError:
                pass
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
        self.value_map      = None
        self._calib_region  = None
        self.mag_tk_img = None
        self._toast_window = None
        self.mode = "wait"
        print("Back to standby.")

if __name__ == "__main__":
    app = ColorPickerApp()
    app.root.mainloop() # アプリをバックグラウンドで待機させ続ける