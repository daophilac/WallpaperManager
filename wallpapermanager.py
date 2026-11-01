"""WallpaperManager - crop images to a target ratio and apply as Windows wallpaper."""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

# Must be called before Qt initialises so GetSystemMetrics returns physical pixels
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import Image, ImageOps
from PySide6.QtCore import (Qt, QSize, QRect, QRectF, QThreadPool,
    QRunnable, QObject, Signal, QTimer)
from PySide6.QtGui import QImage, QPixmap, QColor, QPen, QPainter
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QComboBox, QFileDialog,
    QSplitter, QMessageBox, QSizePolicy)
from PySide6.QtCore import (Qt, QSize, QRect, QRectF, QThreadPool,
    QRunnable, QObject, Signal, QTimer, QByteArray)

SCREEN_W = ctypes.windll.user32.GetSystemMetrics(0)
SCREEN_H = ctypes.windll.user32.GetSystemMetrics(1)

RATIOS = {
    "16:9": 16 / 9,
    "16:10": 16 / 10,
    "21:9": 21 / 9,
    "4:3": 4 / 3,
    "3:2": 3 / 2,
    "1:1": 1.0,
    "9:16": 9 / 16,
    "3:4": 3 / 4,
    "Screen": SCREEN_W / SCREEN_H,
}
DEFAULT_RATIO = "16:9"
FILL_MODES = ("Fill", "Fit")
DEFAULT_FILL_MODE = "Fill"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def set_wallpaper(path: str) -> None:
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
                         winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10")  # Fill
    winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
    winreg.CloseKey(key)
    SPI_SETDESKWALLPAPER = 20
    SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x01, 0x02
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)


class AppPaths:
    """All persistent data lives next to the script / exe."""
    def __init__(self):
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
               else Path(__file__).resolve().parent
        self.data = base / "wallpapermanager_data"
        self.cache = self.data / "cache"
        self.data.mkdir(exist_ok=True)
        self.cache.mkdir(exist_ok=True)
        self.state_file = self.data / "state.json"


class StateManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.data = {"folder": None, "last_image": None,
                     "ratio": DEFAULT_RATIO, "images": {}}
        self._load()

    def _load(self):
        try:
            if self.paths.state_file.exists():
                with open(self.paths.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.paths.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            tmp.replace(self.paths.state_file)
        except Exception:
            pass

    def get_image(self, path):
        return self.data["images"].get(str(path))

    def set_image(self, path, st):
        self.data["images"][str(path)] = st


class _ThumbSignals(QObject):
    done = Signal(str, QImage)


class ThumbTask(QRunnable):
    def __init__(self, path: str, height: int):
        super().__init__()
        self.path = path
        self.height = height
        self.signals = _ThumbSignals()

    def run(self):
        try:
            im = Image.open(self.path)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((self.height * 2, self.height), Image.LANCZOS)
            im = im.convert("RGB")
            data = im.tobytes("raw", "RGB")
            qimg = QImage(data, im.width, im.height, im.width * 3,
                          QImage.Format_RGB888).copy()
        except Exception:
            qimg = QImage()
        self.signals.done.emit(self.path, qimg)


class CropView(QWidget):
    crop_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(400, 300)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.base_img = None
        self.disp_pixmap = None
        self.disp_rect = QRect()
        self.crop = None
        self.aspect = 16 / 9
        self._drag_mode = None
        self._drag_corner = None
        self._drag_start_pos = None
        self._drag_start_crop = None

    # ---------- public API ----------
    def set_image(self, pil_img):
        self.base_img = pil_img
        self.crop = None
        if pil_img is not None:
            im = pil_img
            if im.width > 2400 or im.height > 2400:
                im = im.copy()
                im.thumbnail((2400, 2400), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            data = im.tobytes("raw", "RGB")
            qimg = QImage(data, im.width, im.height, im.width * 3,
                          QImage.Format_RGB888).copy()
            self.disp_pixmap = QPixmap.fromImage(qimg)
        else:
            self.disp_pixmap = None
        self.update()

    def default_crop(self):
        if self.base_img is None:
            return None
        iw, ih = self.base_img.size
        if iw / ih > self.aspect:
            h = ih
            w = h * self.aspect
        else:
            w = iw
            h = w / self.aspect
        return ((iw - w) / 2 / iw, (ih - h) / 2 / ih, w / iw, h / ih)

    def fit_crop(self, crop):
        if self.base_img is None or crop is None:
            return None
        iw, ih = self.base_img.size
        x, y, w, h = crop
        cx, cy = x + w / 2, y + h / 2
        w_px, h_px = w * iw, h * ih
        if w_px / max(h_px, 1e-6) > self.aspect:
            w_px = h_px * self.aspect
        else:
            h_px = w_px / self.aspect
        max_w = min(iw, ih * self.aspect)
        w_px = min(w_px, max_w)
        h_px = w_px / self.aspect
        x_px = max(0.0, min(cx * iw - w_px / 2, iw - w_px))
        y_px = max(0.0, min(cy * ih - h_px / 2, ih - h_px))
        return (x_px / iw, y_px / ih, w_px / iw, h_px / ih)

    def set_aspect(self, ar):
        self.aspect = ar
        if self.base_img is not None:
            self.crop = self.default_crop() if self.crop is None \
                        else self.fit_crop(self.crop)
            self.update()
            self.crop_changed.emit()

    def set_crop_norm(self, crop):
        if self.base_img is None:
            return
        self.crop = self.default_crop() if crop is None else self.fit_crop(crop)
        self.update()

    # ---------- painting ----------
    def _disp_rect_for_paint(self):
        if self.disp_pixmap is None:
            return QRect()
        pw, ph = self.disp_pixmap.width(), self.disp_pixmap.height()
        w, h = self.width(), self.height()
        scale = min(w / pw, h / ph)
        dw, dh = int(pw * scale), int(ph * scale)
        return QRect((w - dw) // 2, (h - dh) // 2, dw, dh)

    def _crop_widget_rect(self):
        dr = self.disp_rect
        x, y, w, h = self.crop
        return QRectF(dr.x() + x * dr.width(), dr.y() + y * dr.height(),
                      w * dr.width(), h * dr.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(25, 25, 28))
        if self.disp_pixmap is None:
            p.setPen(QColor(130, 130, 130))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Select an image from the list")
            p.end()
            return
        self.disp_rect = self._disp_rect_for_paint()
        p.drawPixmap(self.disp_rect, self.disp_pixmap)
        if self.crop:
            cr = self._crop_widget_rect()
            full = QRectF(self.disp_rect)
            ov = QColor(0, 0, 0, 150)
            p.fillRect(QRectF(full.x(), full.y(), full.width(),
                              cr.y() - full.y()), ov)
            p.fillRect(QRectF(full.x(), cr.bottom(), full.width(),
                              full.bottom() - cr.bottom()), ov)
            p.fillRect(QRectF(full.x(), cr.y(), cr.x() - full.x(),
                              cr.height()), ov)
            p.fillRect(QRectF(cr.right(), cr.y(), full.right() - cr.right(),
                              cr.height()), ov)
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
            p.drawRect(cr)
            p.setBrush(QColor(255, 255, 255))
            hs = 8
            for cx, cy in [(cr.left(), cr.top()), (cr.right(), cr.top()),
                           (cr.left(), cr.bottom()), (cr.right(), cr.bottom())]:
                p.drawRect(QRectF(cx - hs / 2, cy - hs / 2, hs, hs))
        p.end()

    # ---------- interaction ----------
    def _hit(self, pos):
        if not self.crop or not self.disp_rect.isValid():
            return None
        cr = self._crop_widget_rect()
        th = 12
        for pt, name in [((cr.left(), cr.top()), "tl"),
                         ((cr.right(), cr.top()), "tr"),
                         ((cr.left(), cr.bottom()), "bl"),
                         ((cr.right(), cr.bottom()), "br")]:
            if abs(pos.x() - pt[0]) <= th and abs(pos.y() - pt[1]) <= th:
                return ("resize", name)
        if cr.contains(pos):
            return ("move", None)
        if QRectF(self.disp_rect).contains(pos):
            return ("recenter", None)
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.crop is None:
            return
        hit = self._hit(e.position())
        if hit is None:
            return
        self.setFocus()
        if hit[0] == "recenter":
            dr = self.disp_rect
            x, y, w, h = self.crop
            cx = (e.position().x() - dr.x()) / dr.width()
            cy = (e.position().y() - dr.y()) / dr.height()
            self.crop = (max(0.0, min(cx - w / 2, 1 - w)),
                         max(0.0, min(cy - h / 2, 1 - h)), w, h)
            self._drag_mode = "move"
            self._drag_start_pos = e.position()
            self._drag_start_crop = self.crop
            self.update()
            self.crop_changed.emit()
            return
        self._drag_mode, self._drag_corner = hit
        self._drag_start_pos = e.position()
        self._drag_start_crop = self.crop

    def mouseMoveEvent(self, e):
        if self.crop is None:
            return
        if self._drag_mode is None:
            hit = self._hit(e.position())
            if hit and hit[0] == "resize":
                self.setCursor(Qt.SizeFDiagCursor if hit[1] in ("tl", "br")
                               else Qt.SizeBDiagCursor)
            elif hit:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        if self._drag_mode == "move":
            dr = self.disp_rect
            dx = (e.position().x() - self._drag_start_pos.x()) / dr.width()
            dy = (e.position().y() - self._drag_start_pos.y()) / dr.height()
            x, y, w, h = self._drag_start_crop
            self.crop = (max(0.0, min(x + dx, 1 - w)),
                         max(0.0, min(y + dy, 1 - h)), w, h)
        else:
            self._resize(e.position())
        self.update()
        self.crop_changed.emit()

    def _resize(self, pos):
        dr = self.disp_rect
        x0, y0, w0, h0 = self._drag_start_crop
        corner = self._drag_corner
        if corner == "tl":
            ax, ay = x0 + w0, y0 + h0
        elif corner == "tr":
            ax, ay = x0, y0 + h0
        elif corner == "bl":
            ax, ay = x0 + w0, y0
        else:
            ax, ay = x0, y0
        iw, ih = self.base_img.size
        mx = (pos.x() - dr.x()) / dr.width()
        my = (pos.y() - dr.y()) / dr.height()
        dx = abs(mx - ax) * iw
        dy = abs(my - ay) * ih
        if dx >= dy * self.aspect:
            w_px, h_px = dx, dx / self.aspect
        else:
            h_px, w_px = dy, dy * self.aspect
        max_w = (ax * iw) if corner in ("tl", "bl") else ((1 - ax) * iw)
        max_h = (ay * ih) if corner in ("tl", "tr") else ((1 - ay) * ih)
        if w_px > max_w:
            w_px, h_px = max_w, max_w / self.aspect
        if h_px > max_h:
            h_px, w_px = max_h, max_h * self.aspect
        if w_px < 8 or h_px < 8:
            return
        w, h = w_px / iw, h_px / ih
        nx = ax - w if corner in ("tl", "bl") else ax
        ny = ay - h if corner in ("tl", "tr") else ay
        self.crop = (nx, ny, w, h)

    def mouseReleaseEvent(self, _):
        self._drag_mode = None
        self._drag_corner = None
        self.setCursor(Qt.ArrowCursor)

    def keyPressEvent(self, e):
        if self.crop is None:
            return
        step = 0.05 if (e.modifiers() & Qt.ShiftModifier) else 0.005
        x, y, w, h = self.crop
        moved = True
        if e.key() == Qt.Key_Left:
            x = max(0.0, x - step)
        elif e.key() == Qt.Key_Right:
            x = min(1 - w, x + step)
        elif e.key() == Qt.Key_Up:
            y = max(0.0, y - step)
        elif e.key() == Qt.Key_Down:
            y = min(1 - h, y + step)
        else:
            moved = False
        if moved:
            self.crop = (x, y, w, h)
            self.update()
            self.crop_changed.emit()
        else:
            super().keyPressEvent(e)


class MainWindow(QMainWindow):
    THUMB_H = 90

    def __init__(self):
        super().__init__()
        self.setWindowTitle("WallpaperManager")
        self.resize(1280, 800)
        self.paths = AppPaths()
        self.state = StateManager(self.paths)
        self.threadpool = QThreadPool.globalInstance()
        self.current_path = None
        self.current_orig = None
        self.current_pil = None
        self._loading = False
        self._item_by_path = {}
        self.fill_mode = DEFAULT_FILL_MODE
        self._build_ui()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_current_state)
        self._restore()
        self._restore_geometry()
        self._cleanup_cache(keep=None)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.btn_folder = QPushButton("Select Folder…")
        self.btn_folder.clicked.connect(self._pick_folder)
        top.addWidget(self.btn_folder)
        self.lbl_folder = QLabel("(no folder)")
        self.lbl_folder.setStyleSheet("color:#888")
        self.lbl_folder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(self.lbl_folder)
        top.addWidget(QLabel("Crop ratio:"))
        self.ratio_combo = QComboBox()
        self.ratio_combo.addItems(list(RATIOS.keys()))
        self.ratio_combo.setCurrentText(DEFAULT_RATIO)
        self.ratio_combo.currentTextChanged.connect(self._on_ratio_changed)
        top.addWidget(self.ratio_combo)
        top.addWidget(QLabel("Mode:"))
        self.fill_combo = QComboBox()
        self.fill_combo.addItems(FILL_MODES)
        self.fill_combo.setCurrentText(DEFAULT_FILL_MODE)
        self.fill_combo.currentTextChanged.connect(self._on_fill_mode_changed)
        top.addWidget(self.fill_combo)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(160, self.THUMB_H))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setSpacing(8)
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(self._on_item_changed)
        split.addWidget(self.list)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.crop_view = CropView()
        self.crop_view.crop_changed.connect(self._on_crop_changed)
        rv.addWidget(self.crop_view, 1)
        bar = QHBoxLayout()
        for label, slot in (("Rotate ↺", lambda: self._rotate(-90)),
                            ("Rotate ↻", lambda: self._rotate(90)),
                            ("Flip H", self._flip_h),
                            ("Flip V", self._flip_v),
                            ("Reset Crop", self._reset_crop)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.btn_apply = QPushButton("Apply as Wallpaper")
        self.btn_apply.setStyleSheet("font-weight:bold; padding:6px 14px;")
        self.btn_apply.clicked.connect(self._apply)
        bar.addWidget(self.btn_apply)
        rv.addLayout(bar)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 900])
        root.addWidget(split, 1)
        self.statusBar().showMessage(f"Screen: {SCREEN_W}×{SCREEN_H}")

    # ---------- folder / thumbnails ----------
    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Choose folder with images", self.state.data.get("folder") or "")
        if d:
            self._load_folder(d, save_state=True)

    def _load_folder(self, folder, save_state):
        if save_state:
            self.state.data["folder"] = folder
            self.state.data["last_image"] = None
            self.state.save()
        self.lbl_folder.setText(folder)
        self.current_path = None
        self.current_orig = None
        self.current_pil = None
        self.crop_view.set_image(None)
        self.list.blockSignals(True)
        self.list.clear()
        self._item_by_path.clear()
        self.list.blockSignals(False)

        files = []
        try:
            files = sorted(f for f in Path(folder).iterdir()
                           if f.is_file() and f.suffix.lower() in IMG_EXTS)
        except Exception:
            pass
        for f in files:
            item = QListWidgetItem(f.name)
            item.setData(Qt.UserRole, str(f))
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            ph = QPixmap(160, self.THUMB_H)
            ph.fill(QColor(45, 45, 48))
            item.setIcon(ph)
            self.list.addItem(item)
            self._item_by_path[str(f)] = item
            task = ThumbTask(str(f), self.THUMB_H)
            task.signals.done.connect(self._on_thumb)
            self.threadpool.start(task)
        if files:
            self.statusBar().showMessage(
                f"Loaded {len(files)} images from {folder}", 5000)

    def _on_thumb(self, path, qimg):
        item = self._item_by_path.get(path)
        if item is not None and not qimg.isNull():
            item.setIcon(QPixmap.fromImage(qimg))

    def _on_item_changed(self, cur, _prev):
        if cur is None:
            return
        path = cur.data(Qt.UserRole)
        if path == self.current_path:
            return
        self._save_current_state()
        self._load_image(path)

    # ---------- image loading ----------
    def _load_image(self, path):
        self._loading = True
        self.current_path = path
        try:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im).convert("RGB")
            self.current_orig = im
            self.current_pil = self._apply_transform(im, path)
            self.crop_view.set_image(self.current_pil)
            st = self.state.get_image(path)
            self.crop_view.set_crop_norm(
                tuple(st["crop"]) if st and st.get("crop") else
                self.crop_view.default_crop())
            self.state.data["last_image"] = path
            self.state.save()
            self.setWindowTitle(f"WallpaperManager — {Path(path).name}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load:\n{path}\n\n{e}")
            self.current_orig = None
            self.current_pil = None
            self.crop_view.set_image(None)
        self._loading = False

    def _apply_transform(self, im, path):
        st = self.state.get_image(path)
        if not st:
            return im
        t = st.get("transform") or {}
        rot = int(t.get("rotate", 0)) % 360
        if rot:
            im = im.rotate(-rot, expand=True)
        if t.get("flip_h"):
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
        if t.get("flip_v"):
            im = im.transpose(Image.FLIP_TOP_BOTTOM)
        return im

    def _ensure_state(self, path):
        st = self.state.get_image(path)
        if st is None:
            st = {"mtime": time.time(),
                  "transform": {"rotate": 0, "flip_h": False, "flip_v": False},
                  "crop": None}
            self.state.set_image(path, st)
        return st

    def _reload_current(self):
        if self.current_orig is None or self.current_path is None:
            return
        self.current_pil = self._apply_transform(self.current_orig,
                                                 self.current_path)
        old = self.crop_view.crop
        self.crop_view.set_image(self.current_pil)
        self.crop_view.set_crop_norm(
            old if old else self.crop_view.default_crop())

    # ---------- actions ----------
    def _rotate(self, delta):
        if self.current_path is None:
            return
        st = self._ensure_state(self.current_path)
        st["transform"]["rotate"] = \
            (int(st["transform"].get("rotate", 0)) + delta) % 360
        self._reload_current()
        self._save_current_state()

    def _flip_h(self):
        if self.current_path is None:
            return
        st = self._ensure_state(self.current_path)
        st["transform"]["flip_h"] = not st["transform"].get("flip_h", False)
        self._reload_current()
        self._save_current_state()

    def _flip_v(self):
        if self.current_path is None:
            return
        st = self._ensure_state(self.current_path)
        st["transform"]["flip_v"] = not st["transform"].get("flip_v", False)
        self._reload_current()
        self._save_current_state()

    def _reset_crop(self):
        if self.crop_view.base_img is None:
            return
        self.crop_view.set_crop_norm(self.crop_view.default_crop())
        self._save_current_state()

    def _on_ratio_changed(self, key):
        if self._loading:
            return
        self.state.data["ratio"] = key
        self.crop_view.set_aspect(RATIOS.get(key, 16 / 9))
        self._save_current_state()

    def _on_fill_mode_changed(self, mode):
        self.fill_mode = mode
        self.state.data["fill_mode"] = mode
        self.state.save()

    def _on_crop_changed(self):
        if not self._loading:
            self._save_timer.start()

    def _save_current_state(self):
        if self.current_path is None or self.crop_view.crop is None:
            return
        st = self._ensure_state(self.current_path)
        st["crop"] = list(self.crop_view.crop)
        st["mtime"] = time.time()
        self.state.data["last_image"] = self.current_path
        self.state.save()

    def _apply(self):
        if self.current_pil is None or self.crop_view.crop is None:
            QMessageBox.information(self, "No image", "Select an image first.")
            return
        self._save_current_state()
        crop = self.crop_view.crop
        iw, ih = self.current_pil.size
        x = max(0, min(int(round(crop[0] * iw)), iw - 1))
        y = max(0, min(int(round(crop[1] * ih)), ih - 1))
        w = max(1, min(int(round(crop[2] * iw)), iw - x))
        h = max(1, min(int(round(crop[3] * ih)), ih - y))
        region = self.current_pil.crop((x, y, x + w, y + h))

        if self.fill_mode == "Fit":
            scale = min(SCREEN_W / region.width, SCREEN_H / region.height)
            nw = max(1, int(round(region.width * scale)))
            nh = max(1, int(round(region.height * scale)))
            region = region.resize((nw, nh), Image.LANCZOS)
            final = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
            final.paste(region, ((SCREEN_W - nw) // 2, (SCREEN_H - nh) // 2))
        else:  # Fill
            final = ImageOps.fit(region, (SCREEN_W, SCREEN_H), Image.LANCZOS)

        if final.mode != "RGB":
            final = final.convert("RGB")
        out = self.paths.cache / f"wallpaper_{int(time.time() * 1000)}.bmp"
        final.save(str(out), "BMP")
        try:
            set_wallpaper(str(out.resolve()))
        except Exception as e:
            QMessageBox.warning(self, "Wallpaper error",
                                f"Could not set wallpaper:\n{e}")
            return
        self._cleanup_cache(keep=out)
        self.statusBar().showMessage(
            f"Applied {Path(self.current_path).name}  "
            f"[{self.fill_mode}] ({w}×{h} crop → {SCREEN_W}×{SCREEN_H})", 8000)

    def _cleanup_cache(self, keep):
        try:
            for f in self.paths.cache.glob("wallpaper_*.bmp"):
                if keep is not None and f.resolve() == keep.resolve():
                    continue
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- startup / shutdown ----------
    def _restore(self):
        folder = self.state.data.get("folder")
        ratio = self.state.data.get("ratio", DEFAULT_RATIO)
        if ratio not in RATIOS:
            ratio = DEFAULT_RATIO
        fm = self.state.data.get("fill_mode", DEFAULT_FILL_MODE)
        if fm not in FILL_MODES:
            fm = DEFAULT_FILL_MODE
        self.fill_mode = fm
        self.fill_combo.setCurrentText(fm)
        self._loading = True
        self.ratio_combo.setCurrentText(ratio)
        self.crop_view.aspect = RATIOS[ratio]
        self._loading = False
        if folder and Path(folder).is_dir():
            self.lbl_folder.setText(folder)
            self._load_folder(folder, save_state=False)
            last = self.state.data.get("last_image")
            if last and last in self._item_by_path:
                self.list.setCurrentItem(self._item_by_path[last])

    def _save_geometry(self):
        try:
            b64 = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.state.data["geometry"] = b64
        except Exception:
            pass

    def _restore_geometry(self):
        g = self.state.data.get("geometry")
        if not g:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(g.encode("ascii")))
        except Exception:
            pass

    def closeEvent(self, e):
        self._save_current_state()
        self._save_geometry()
        self.state.save()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()