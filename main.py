"""
電腦檔案管理系統 v1.0
========================
功能摘要：
  - 支援最多 4 個資料夾同時瀏覽（四宮格面板）
  - 以 SHA-256 雜湊比對重複檔案（支援跨目錄）
  - 重複檔案可一鍵移至回收筒或「鏡像移動」至指定目錄
  - 移動/刪除後自動偵測並清理空資料夾（可選）
  - 圖片縮圖即時預覽（支援 jpg/png/gif/bmp/webp/tif/heic）
  - 副檔名篩選器（相片、影片、音訊、文件、壓縮檔、自訂）
  - 視窗大小記憶（重開後自動還原上次尺寸）

技術棧：PyQt6 + hashlib（SHA-256）+ send2trash + shutil
版本：1.0
"""

import sys
import os
import hashlib
import sqlite3
import shutil
import time
from send2trash import send2trash
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout,
    QHBoxLayout, QPushButton, QTreeView, QHeaderView, QLabel,
    QFileDialog, QDialog, QTreeWidget, QTreeWidgetItem, QMessageBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QProgressDialog, QComboBox,
    QSplitter
)
from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt, QSettings
from PyQt6.QtGui import QFileSystemModel, QFont, QPixmap


# 支援圖片預覽的副檔名集合（用於 ConvergenceDialog 的縮圖預覽功能）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif"}

APP_STYLESHEET = """
QMainWindow, QDialog {
    background: #f4f7ff;
    color: #1f2937;
    font-family: "Segoe UI", "Microsoft JhengHei", sans-serif;
    font-size: 13px;
}

QWidget#MainSurface {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #f8faff, stop:0.55 #eef3ff, stop:1 #fff2f7);
}

QWidget#PanelCard {
    background: #ffffff;
    border: 1px solid #dce5ff;
    border-radius: 14px;
}

QLabel#PanelTitle {
    color: #2e3f8f;
    font-weight: 700;
    font-size: 14px;
    padding: 4px 4px 2px 4px;
}

QLabel#StatusBadge {
    color: #19435f;
    background: #d9f1ff;
    border: 1px solid #a8defa;
    border-radius: 10px;
    padding: 5px 10px;
    font-weight: 600;
}

QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5f6bff, stop:1 #7a4dff);
    color: white;
    border: 0;
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 600;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4f5af0, stop:1 #6d45e6);
}

QPushButton:pressed {
    background: #4a42c8;
}

QLineEdit, QComboBox, QTreeView, QTreeWidget, QTableWidget {
    background: #ffffff;
    border: 1px solid #d8def3;
    border-radius: 10px;
    padding: 6px;
}

QTreeView::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {
    background: #dfe6ff;
    color: #1a1f36;
}

QHeaderView::section {
    background: #edf1ff;
    color: #374151;
    border: 0;
    border-bottom: 1px solid #d8def3;
    padding: 6px;
    font-weight: 600;
}

QProgressDialog {
    background: #f8faff;
    border-radius: 12px;
}

QMessageBox {
    background: #f9fbff;
}
"""

# ==========================================
# 1. 非同步掃描線程 (效能優化版)
# ==========================================
class ScanWorker(QObject):
    """
    在獨立 QThread 中執行檔案掃描與 SHA-256 雜湊比對。

    訊號說明：
        progress_val : 每處理一個檔案就發送目前完成數量（int）
        progress_max : 掃描開始前發送檔案總數（int），用於設定進度條上限
        status_text  : 每處理一個檔案就發送目前檔名字串，顯示於進度對話框
        finished     : 掃描結束後發送結果，格式為 {sha256_hex: [路徑, ...]}（dict）
    """
    progress_val = pyqtSignal(int)      # 當前完成數
    progress_max = pyqtSignal(int)      # 總檔案數
    status_text = pyqtSignal(str)       # 當前檔名
    finished = pyqtSignal(dict)         # 結束傳回重複清單 {hash: [path, ...]}

    def __init__(self, paths, db):
        """
        初始化掃描工作器。

        Args:
            paths: 要掃描的資料夾路徑，可為字串或字串清單。
            db   : 資料庫連線（目前版本保留參數但未使用，設為 None 即可）。
        """
        super().__init__()
        if isinstance(paths, str):
            paths = [paths]
        self.paths = [os.path.normpath(p) for p in paths if p]
        self.db = db
        self._is_cancelled = False  # 取消旗標，由主執行緒呼叫 cancel() 設定

    def cancel(self):
        """設定取消旗標，使 run() 在下一個安全點提前結束。"""
        self._is_cancelled = True

    def calculate_hash(self, filepath):
        """
        計算單一檔案的 SHA-256 雜湊值。

        採用分塊讀取（每次 1 MB），避免大檔案佔用過多記憶體。
        若使用者已取消或檔案無法開啟，則回傳 None。

        Args:
            filepath: 要計算雜湊的檔案完整路徑。

        Returns:
            str: 64 字元的十六進位雜湊字串；失敗或取消時回傳 None。
        """
        sha256 = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                while True:
                    if self._is_cancelled:
                        return None  # 已取消，立即中止
                    chunk = f.read(1024 * 1024)  # 每次讀 1 MB
                    if not chunk:
                        break
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception:
            return None  # 無法讀取（權限不足、檔案損毀等）

    def run(self):
        """
        掃描工作主流程（由 QThread.started 訊號觸發）。

        步驟：
          1. 遞迴收集所有路徑下的檔案清單（排除重複路徑）
          2. 逐一計算 SHA-256 雜湊，並以雜湊為鍵分組
          3. 掃描完成後透過 finished 訊號回傳結果字典
        """
        all_files = []
        seen_files = set()  # 防止同一檔案因路徑重疊被重複計算

        # 步驟 1：遞迴收集所有路徑下的檔案（包含根層和所有子資料夾）
        for base_path in self.paths:
            for r, _, fs in os.walk(base_path):
                if self._is_cancelled:
                    return
                for f in fs:
                    fp = os.path.normpath(os.path.join(r, f))
                    if fp not in seen_files:
                        seen_files.add(fp)
                        all_files.append(fp)

        total = len(all_files)
        self.progress_max.emit(total)  # 通知 UI 設定進度條上限

        dups = {}  # 結果字典：{sha256_hex: [file_path, ...]}

        # 步驟 2：逐一計算 SHA-256 並分組
        for i, p in enumerate(all_files):
            if self._is_cancelled:
                break

            p = os.path.normpath(p)
            self.status_text.emit(f"處理中 ({i+1}/{total}): {os.path.basename(p)}")
            self.progress_val.emit(i + 1)

            try:
                os.stat(p)  # 確認檔案存在（stat 呼叫失敗即跳過）
                f_hash = self.calculate_hash(p)
                if f_hash:
                    dups.setdefault(f_hash, []).append(p)
            except Exception:
                continue  # 無法存取的檔案直接略過

        # 步驟 3：掃描完成，回傳結果（使用者取消則不發送 finished 訊號）
        if not self._is_cancelled:
            self.finished.emit(dups)

# ==========================================
# 2. UI 組件 (保留之前的對話框類別)
# ==========================================
def _find_source_root(filepath, roots):
    """
    從多個根目錄清單中找出包含 filepath 的「最深層」根目錄。

    例如：roots = ['D:\\Photos', 'D:\\Photos\\2024']，filepath 位於
    'D:\\Photos\\2024\\img.jpg'，則回傳 'D:\\Photos\\2024'（最長匹配）。
    若找不到任何匹配，則回傳 filepath 的父目錄。

    Args:
        filepath: 目標檔案的完整路徑。
        roots   : 所有已載入面板的根目錄路徑清單。

    Returns:
        str: 最深層匹配的根目錄路徑。
    """
    norm_f = os.path.normpath(filepath)
    best = None
    for r in roots:
        try:
            if os.path.commonpath([norm_f, r]) == r:
                if best is None or len(r) > len(best):  # 取最深（最長）匹配
                    best = r
        except ValueError:
            continue  # 跨磁碟機路徑比較會拋出 ValueError，直接略過
    return best if best else os.path.dirname(norm_f)


def _safe_relpath(filepath, source_root):
    """
    計算 filepath 相對於 source_root 的相對路徑，且不會拋出例外。

    處理跨磁碟機（如 C:\\ vs D:\\）或路徑超出根目錄等邊界情況：
    若計算結果以 '..' 開頭（表示超出根目錄範圍），則退回只用檔名。

    Args:
        filepath   : 目標檔案的完整路徑。
        source_root: 用於計算相對路徑的基準根目錄。

    Returns:
        str: 相對路徑字串；無法計算時回傳純檔名。
    """
    try:
        rel = os.path.relpath(filepath, source_root)
        # 若路徑以 '..' 開頭，表示檔案超出 source_root 範圍，改用純檔名
        if rel.startswith('..'):
            return os.path.basename(filepath)
        return rel
    except ValueError:
        return os.path.basename(filepath)  # 跨磁碟機時 relpath 會拋出 ValueError


class MovePreviewDialog(QDialog):
    """
    鏡像移動預覽對話框。

    在實際移動前，以表格呈現每個檔案的「原始位置」與「目標位置（鏡像）」，
    讓使用者確認路徑映射是否正確後再執行。

    「鏡像移動」規則：
        保留每個檔案相對於其來源根目錄的相對路徑，
        並在目標根目錄下重建相同的目錄結構。
    """
    def __init__(self, file_paths, source_roots, target_root, parent=None):
        """
        Args:
            file_paths  : 要移動的檔案路徑清單。
            source_roots: 所有已載入面板的根目錄路徑清單（用於計算相對路徑）。
            target_root : 移動目的地的根目錄路徑。
            parent      : 父視窗（QWidget），預設為 None。
        """
        super().__init__(parent)
        self.setWindowTitle("鏡像移動預覽")
        self.resize(1000, 500)
        self.file_paths = file_paths
        self.source_roots = [os.path.normpath(r) for r in source_roots]
        self.target_root = os.path.normpath(target_root)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>預計將 {len(self.file_paths)} 個檔案移動至：</b><br>{self.target_root}"))
        self.table = QTableWidget(len(self.file_paths), 2)
        self.table.setHorizontalHeaderLabels(["原始位置", "目標位置 (鏡像)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, src in enumerate(self.file_paths):
            self.table.setItem(row, 0, QTableWidgetItem(src))
            src_root = _find_source_root(src, self.source_roots)
            rel = _safe_relpath(src, src_root)
            dst = os.path.normpath(os.path.join(self.target_root, rel))
            item_dst = QTableWidgetItem(dst)
            item_dst.setForeground(Qt.GlobalColor.blue)
            self.table.setItem(row, 1, item_dst)
        layout.addWidget(self.table)
        btns = QHBoxLayout(); btn_ok = QPushButton("確認移動"); btn_ok.clicked.connect(self.accept)
        btn_no = QPushButton("取消"); btn_no.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(btn_no); btns.addWidget(btn_ok); layout.addLayout(btns)

class ConvergenceDialog(QDialog):
    """
    重複檔案收斂中心對話框。

    顯示 ScanWorker 回傳的重複檔案分組，並提供以下操作：
      - 勾選要處理的重複副本
      - 副檔名篩選器（快速聚焦特定類型）
      - 圖片即時預覽（點擊清單中的圖片項目）
      - 移至回收筒 / 鏡像移動至指定目錄

    判定邏輯：
      - 位於「視窗 1（主目錄）」下的檔案：標記為「建議保留」，預設不勾選
      - 位於其他目錄下的重複副本：標記為「建議收斂」，預設勾選
    """
    def __init__(self, dup_dict, main_folder, parent=None):
        """
        Args:
            dup_dict   : 重複檔案字典，格式 {sha256_hex: [file_path, ...]}。
            main_folder: 視窗 1 的根目錄路徑（作為「保留基準目錄」）。
            parent     : 父視窗（QWidget），預設為 None。
        """
        super().__init__(parent)
        self.setWindowTitle("重複檔案收斂中心")
        self.resize(1000, 600)
        self.main_folder = os.path.normpath(main_folder)
        self.dup_dict = dup_dict
        self.action_type = None    # 'trash' 或 'move'，由使用者按下按鈕決定
        self.selected_files = []   # 使用者確認後，儲存被勾選的檔案路徑清單
        self.init_ui()

    def _is_under_main_folder(self, path):
        """判斷 path 是否位於主目錄（視窗 1）之下。"""
        try:
            return os.path.commonpath([path, self.main_folder]) == self.main_folder
        except Exception:
            return False

    def _get_filter_exts(self):
        """
        依據篩選下拉選單的目前選項，回傳對應的副檔名集合。

        Returns:
            None  : 選擇「全部檔案」時，表示不篩選。
            set   : 包含小寫副檔名（含點號）的集合，例如 {'.jpg', '.png'}。
                    若選擇「自訂副檔名」但輸入框為空，則回傳空集合（隱藏全部）。
        """
        mode = self.filter_combo.currentText()
        if mode == "全部檔案":
            return None
        if mode == "相片檔":
            return {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff"}
        if mode == "影片檔":
            return {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v"}
        if mode == "音訊檔":
            return {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
        if mode == "文件檔":
            return {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"}
        if mode == "壓縮檔":
            return {".zip", ".rar", ".7z", ".tar", ".gz"}
        # 自訂副檔名
        raw = self.custom_ext_input.text().strip()
        if not raw:
            return set()
        exts = set()
        for part in raw.replace(";", ",").split(","):
            e = part.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            exts.add(e)
        return exts

    def apply_tree_filter(self):
        """
        依據目前的篩選條件，顯示/隱藏樹狀清單中的項目，並更新已勾選計數。

        邏輯：
          - 遍歷所有「重複組」及其子項目（個別檔案）
          - 根據副檔名決定顯示或隱藏
          - 若整組所有子項目都被隱藏，則也隱藏該組標題列
          - 更新底部「已勾選 N 個檔案」的標籤
        """
        selected_exts = self._get_filter_exts()
        checked_count = 0
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            visible_count = 0
            for j in range(group.childCount()):
                item = group.child(j)
                path = item.text(0)
                ext = os.path.splitext(path)[1].lower()
                is_visible = True if selected_exts is None else ext in selected_exts
                item.setHidden(not is_visible)
                if is_visible:
                    visible_count += 1
                    if item.checkState(0) == Qt.CheckState.Checked:
                        checked_count += 1
            group.setHidden(visible_count == 0)  # 整組沒有可見子項目時隱藏組標題
        if hasattr(self, 'count_label'):
            self.count_label.setText(f"已勾選 {checked_count} 個檔案")

    def on_filter_changed(self, _):
        is_custom = self.filter_combo.currentText() == "自訂副檔名"
        self.custom_ext_input.setEnabled(is_custom)
        QSettings("FileManagerApp", "ConvergenceDialog").setValue("filter_mode", self.filter_combo.currentText())
        self.apply_tree_filter()

    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # 狀態說明
        info_label = QLabel(f"<b>比對基準目錄：</b> {self.main_folder}")
        layout.addWidget(info_label)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("篩選條件:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部檔案", "相片檔", "影片檔", "音訊檔", "文件檔", "壓縮檔", "自訂副檔名"])
        saved_mode = QSettings("FileManagerApp", "ConvergenceDialog").value("filter_mode", "全部檔案")
        idx = self.filter_combo.findText(saved_mode)
        if idx >= 0:
            self.filter_combo.setCurrentIndex(idx)
        self.filter_combo.currentTextChanged.connect(self.on_filter_changed)
        self.custom_ext_input = QLineEdit()
        self.custom_ext_input.setPlaceholderText("輸入副檔名，例如：.jpg,.png")
        self.custom_ext_input.setEnabled(self.filter_combo.currentText() == "自訂副檔名")
        self.custom_ext_input.textChanged.connect(lambda _: self.apply_tree_filter())
        filter_bar.addWidget(self.filter_combo)
        filter_bar.addWidget(self.custom_ext_input)
        layout.addLayout(filter_bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["檔案路徑", "處理建議"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.tree.setColumnWidth(0, 760)
        self.tree.setColumnWidth(1, 220)
        
        # --- 數據加載邏輯 (縮排已修正) ---
        has_duplicates = False
        if self.dup_dict:
            for f_hash, paths in self.dup_dict.items():
                # 只有當同一個 Hash 下有超過一個檔案時才顯示
                if len(paths) > 1:
                    has_duplicates = True
                    group = QTreeWidgetItem(self.tree)
                    group.setText(0, f"重複組 (SHA256: {f_hash[:12]}...)")
                    group.setText(1, f"找到 {len(paths)} 個檔案")
                    group.setToolTip(0, f"SHA256: {f_hash}")
                    group.setToolTip(1, f"重複數量: {len(paths)}")
                    group.setExpanded(True)
                    
                    for p in paths:
                        norm_p = os.path.normpath(p)
                        child = QTreeWidgetItem(group)
                        child.setText(0, norm_p)
                        child.setToolTip(0, norm_p)
                        child.setToolTip(1, os.path.basename(norm_p))
                        
                        # 判定邏輯：是否為主目錄下的檔案
                        if self._is_under_main_folder(norm_p):
                            child.setText(1, "✅ 建議保留 (位於主目錄)")
                            child.setCheckState(0, Qt.CheckState.Unchecked)
                            # 加粗字體突出顯示
                            font = QFont()
                            font.setBold(True)
                            child.setFont(0, font)
                        else:
                            child.setText(1, "⚠️ 建議收斂 (重複副本)")
                            child.setCheckState(0, Qt.CheckState.Checked)

        if not has_duplicates:
            self.tree.addTopLevelItem(QTreeWidgetItem(["(未發現任何重複檔案)", ""]))

        # ---- 右側預覽面板 ----
        self._preview_panel = QWidget()
        self._preview_panel.setObjectName("PanelCard")
        pv_layout = QVBoxLayout(self._preview_panel)
        pv_title = QLabel("🖼️ 圖片預覽")
        pv_title.setObjectName("PanelTitle")
        pv_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pv_layout.addWidget(pv_title)

        self._preview_label = QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(220, 220)
        self._preview_label.setStyleSheet("background:#f4f7ff; border-radius:8px;")
        pv_layout.addWidget(self._preview_label)

        self._preview_info = QLabel("點擊圖片檔以預覽")
        self._preview_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_info.setWordWrap(True)
        self._preview_info.setStyleSheet("color:#556; font-size:11px; padding:4px;")
        pv_layout.addWidget(self._preview_info)
        pv_layout.addStretch()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(self._preview_panel)
        splitter.setSizes([720, 260])
        splitter.setChildrenCollapsible(False)

        self.tree.currentItemChanged.connect(self._update_preview)

        self.apply_tree_filter()
        layout.addWidget(splitter)

        # 檢視計數 label + 功能按鈕區
        btns = QHBoxLayout()
        self.count_label = QLabel("\u5df2勾選 0 個檔案")
        self.count_label.setObjectName("StatusBadge")
        self.tree.itemChanged.connect(lambda _: self.apply_tree_filter())
        self.apply_tree_filter()  # init count
        btn_trash = QPushButton("🗑️ 移至回收筒")
        btn_trash.setStyleSheet("padding: 8px;")
        btn_trash.clicked.connect(lambda: self.finish('trash'))
        
        btn_move = QPushButton("📦 鏡像移動至...")
        btn_move.setStyleSheet("padding: 8px;")
        btn_move.clicked.connect(lambda: self.finish('move'))
        
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addWidget(btn_trash)
        btns.addWidget(btn_move)
        btns.addStretch()
        btns.addWidget(self.count_label)
        btns.addWidget(btn_cancel)
        layout.addLayout(btns)

    def _update_preview(self, current, _previous):
        if current is None or current.parent() is None:
            self._preview_label.clear()
            self._preview_info.setText("點擊圖片檔以預覽")
            return
        path = current.text(0)
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS:
            self._preview_label.clear()
            self._preview_info.setText("(非圖片檔不支援預覽)")
            return
        try:
            pixmap = QPixmap(path)
            if pixmap.isNull():
                self._preview_label.clear()
                self._preview_info.setText("無法讀取圖片")
                return
            panel_w = max(self._preview_panel.width() - 24, 200)
            scaled = pixmap.scaled(
                panel_w, panel_w,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self._preview_label.setPixmap(scaled)
            try:
                size_bytes = os.path.getsize(path)
                size_str = (f"{size_bytes/1024:.1f} KB" if size_bytes < 1024*1024
                            else f"{size_bytes/(1024*1024):.2f} MB")
            except OSError:
                size_str = "?"
            self._preview_info.setText(
                f"{os.path.basename(path)}\n"
                f"{pixmap.width()} × {pixmap.height()} px\n{size_str}"
            )
        except Exception:
            self._preview_label.clear()
            self._preview_info.setText("載入失敗")

    def finish(self, action):
        """
        收集被勾選的檔案清單並關閉對話框（以 Accepted 狀態）。

        只收集目前「可見」且「勾選」的項目，被篩選器隱藏的項目不納入。
        若無任何已勾選檔案，顯示警告並不關閉對話框。

        Args:
            action: 操作類型字串，'trash' 表示移至回收筒，'move' 表示鏡像移動。
        """
        self.selected_files = []
        root = self.tree.invisibleRootItem()
        # 遍歷 Tree 收集所有「可見且被勾選」的子項目路徑
        for i in range(root.childCount()):
            group = root.child(i)
            if group.isHidden():  # 整組被篩選器隱藏，跳過
                continue
            for j in range(group.childCount()):
                item = group.child(j)
                if not item.isHidden() and item.checkState(0) == Qt.CheckState.Checked:
                    self.selected_files.append(item.text(0))
        
        if not self.selected_files:
            QMessageBox.warning(self, "提示", "請至少勾選一個要處理的重複檔案。")
            return
            
        self.action_type = action  # 記錄操作類型，供主視窗 on_done() 判斷
        self.accept()

# ==========================================
# 3. 主視窗
# ==========================================
class MainWindow(QMainWindow):
    """
    應用程式主視窗。

    功能：
      - 最多 4 個資料夾面板（2x2 宮格），每格包含 QFileSystemModel 樹狀瀏覽器
      - 工具列：加入資料夾、重置、開始比對
      - 即時搜尋列（透過 QFileSystemModel 的 nameFilters 過濾）
      - 視窗尺寸記憶（使用 QSettings 儲存）
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python 檔案管理大師 - 穩定進化版")
        # 使用 QSettings 記憶並還原上次視窗大小
        self._settings = QSettings("FileManagerApp", "MainWindow")
        w = int(self._settings.value("width", 1200))
        h = int(self._settings.value("height", 800))
        self.resize(w, h)
        self.setup_ui()

    def closeEvent(self, event):
        """視窗關閉時，將目前寬高儲存至 QSettings，下次開啟時還原。"""
        self._settings.setValue("width", self.width())
        self._settings.setValue("height", self.height())
        super().closeEvent(event)

    def setup_ui(self):
        main_wid = QWidget(); self.setCentralWidget(main_wid); main_wid.setObjectName("MainSurface"); layout = QVBoxLayout(main_wid)
        tbar = QHBoxLayout(); btn_add = QPushButton("➕ 加入資料夾"); btn_add.clicked.connect(self.add_dir)
        btn_reset_main = QPushButton("重置資料夾")
        btn_reset_main.setToolTip("清空目前已加入的資料夾，恢復初始狀態")
        btn_reset_main.clicked.connect(self.reset_main_dir)
        btn_go = QPushButton("🚀 開始比對"); btn_go.clicked.connect(self.start_scan)
        self.status = QLabel("狀態: 待命"); self.status.setObjectName("StatusBadge"); tbar.addWidget(btn_add); tbar.addWidget(btn_reset_main); tbar.addWidget(btn_go); tbar.addStretch(); tbar.addWidget(self.status); layout.addLayout(tbar)
        self.search = QLineEdit(); self.search.setPlaceholderText("🔍 即時搜尋檔案..."); self.search.textChanged.connect(self.do_filter); layout.addWidget(self.search)
        self.grid = QGridLayout(); self.panels = []
        for i in range(4):
            p = QWidget(); l = QVBoxLayout(p); title = QLabel(f"視窗 {i+1}"); tree = QTreeView(); model = QFileSystemModel()
            p.setObjectName("PanelCard")
            title.setObjectName("PanelTitle")
            tree.setModel(model); l.addWidget(title); l.addWidget(tree)
            self.panels.append({'widget': p, 'label': title, 'model': model, 'tree': tree})
            self.grid.addWidget(p, i//2, i%2)
            if i > 0: p.hide()
        layout.addLayout(self.grid)

    def add_dir(self):
        """
        開啟目錄選擇對話框，並將所選路徑指定給下一個空閒面板（最多 4 個）。
        面板順序：視窗 1 優先，依序填入未使用的面板。
        """
        path = QFileDialog.getExistingDirectory(self, "選取目錄")
        if path:
            # 找第一個標題仍為「視窗 N」（未指定路徑）的面板
            p = next((x for x in self.panels if not x['widget'].isVisible() or x['label'].text().startswith("視窗")), None)
            if not p:
                QMessageBox.information(self, "提示", "最多只能加入 4 個資料夾。")
                return
            p['label'].setText(os.path.normpath(path)); p['model'].setRootPath(path); p['tree'].setRootIndex(p['model'].index(path)); p['widget'].show()
            self.status.setText("狀態: 已加入資料夾")

    def reset_main_dir(self):
        """
        清除所有面板的路徑設定，恢復至初始「待命」狀態。
        視窗 1 保持顯示（空白），視窗 2~4 隱藏，搜尋列清空。
        """
        for i, p in enumerate(self.panels):
            p['label'].setText(f"視窗 {i+1}")
            p['model'].setRootPath("")
            p['tree'].setRootIndex(p['model'].index(""))
            if i > 0:
                p['widget'].hide()

        self.panels[0]['widget'].show()
        self.search.clear()
        self.status.setText("狀態: 待命")

    def _get_active_panel_roots(self):
        """
        取得所有已指定有效路徑的面板根目錄清單（去除重複，保留順序）。

        Returns:
            list[str]: 正規化後的根目錄路徑清單。
        """
        roots = []
        for p in self.panels:
            panel_root = p['model'].rootPath()
            if panel_root and panel_root != "." and os.path.isdir(panel_root):
                roots.append(os.path.normpath(panel_root))
        return list(dict.fromkeys(roots))  # 以 dict 去除重複，同時保留順序

    def _collect_initial_empty_dirs(self, moved_files, protected_roots):
        """收集移動後的來源資料夾候選清單（不用 os.listdir 預篩，避免 Windows 目錄快取誤判）。"""
        protected_set = set(os.path.normpath(r) for r in protected_roots)
        candidates = set()

        for f in moved_files:
            cur = os.path.normpath(os.path.dirname(f))
            while cur and cur not in protected_set:
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                if os.path.isdir(cur):
                    candidates.add(cur)
                cur = parent

        # 先處理最深層，避免父層先刪造成流程中斷
        sorted_dirs = sorted(candidates, key=lambda x: x.count(os.sep), reverse=True)
        return sorted_dirs, protected_set

    def _delete_empty_dirs_cascade(self, initial_empty_dirs, protected_set):
        """從初始空資料夾開始往上刪除，直到父層非空或受保護為止。
        直接用 os.rmdir 嘗試刪除（失敗表示非空），避免 Windows 目錄快取誤判。"""
        deleted = 0
        visited = set()

        for start_dir in initial_empty_dirs:
            cur = os.path.normpath(start_dir)
            while cur and cur not in protected_set and cur not in visited:
                visited.add(cur)
                parent = os.path.normpath(os.path.dirname(cur))
                if parent == cur:  # 已到達磁碟根目錄
                    break
                try:
                    os.rmdir(cur)  # 若非空會拋出 OSError，自然停止
                    deleted += 1
                    cur = parent   # 成功後繼續往上嘗試父層
                except OSError:
                    break          # 非空或無權限，停止往上

        return deleted

    def _release_panel_watchers(self):
        """暫時讓所有面板 model 卸載路徑，釋放 Windows 檔案系統 watcher handle。
        回傳原始路徑清單，供 _restore_panel_watchers 還原用。"""
        saved = []
        for p in self.panels:
            rp = p['model'].rootPath()
            saved.append(rp)
            if rp and rp != ".":
                p['model'].setRootPath("")
        return saved

    def _restore_panel_watchers(self, saved_roots):
        """還原各面板 model 的監看路徑。"""
        for p, rp in zip(self.panels, saved_roots):
            if rp and rp != ".":
                p['model'].setRootPath(rp)
                p['tree'].setRootIndex(p['model'].index(rp))

    def do_filter(self, text):
        """
        即時搜尋回呼函式：依輸入文字過濾所有面板的檔案顯示。
        使用 QFileSystemModel 的 nameFilters（萬用字元 *text*）實現。
        清空搜尋列時，移除所有篩選器以顯示所有檔案。
        """
        for p in self.panels:
            p['model'].setNameFilters([f"*{text}*"] if text else [])
            p['model'].setNameFilterDisables(False)  # 隱藏不符合的項目（而非灰色顯示）

    def start_scan(self):
        """
        啟動非同步重複檔案掃描。

        前置檢查：視窗 1 必須已設定有效路徑（作為主目錄/比對基準）。
        流程：建立 QThread + ScanWorker，串接所有進度訊號，
        顯示可取消的模態進度對話框。
        """
        root = self.panels[0]['model'].rootPath()
        if not root or root == ".": 
            QMessageBox.warning(self, "警告", "請先設定視窗 1 的資料夾作為主目錄。")
            return

        unique_scan_roots = self._get_active_panel_roots()
        if not unique_scan_roots:
            QMessageBox.warning(self, "警告", "找不到可掃描的資料夾。")
            return

        # 初始化進度條
        self.progress_dlg = QProgressDialog("準備掃描檔案...", "取消", 0, 100, self)
        self.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dlg.setMinimumDuration(0)
        self.progress_dlg.setWindowTitle("正在比對重複檔案")
        self.progress_dlg.resize(500, 150)

        self.thread = QThread()
        self.worker = ScanWorker(unique_scan_roots, None) # DB 暫設 None
        self.worker.moveToThread(self.thread)
        
        # 串接信號
        self.thread.started.connect(self.worker.run)
        self.worker.progress_max.connect(self.progress_dlg.setMaximum)
        self.worker.progress_val.connect(self.progress_dlg.setValue)
        self.worker.status_text.connect(self.progress_dlg.setLabelText)
        self.worker.finished.connect(self.on_done)
        
        # 處理取消
        self.progress_dlg.canceled.connect(self.worker.cancel)
        self.progress_dlg.canceled.connect(self.thread.quit)

        self.thread.start()

    def on_done(self, dups):
        """
        ScanWorker.finished 訊號的回呼。

        關閉進度對話框並開啟 ConvergenceDialog（重複檔案收斂中心）。
        依使用者選擇的操作（'trash' 或 'move'）執行對應的後處理流程，
        並在操作完成後提示是否清理連帶產生的空資料夾。

        Args:
            dups: ScanWorker 回傳的重複檔案字典 {sha256_hex: [path, ...]}。
        """
        self.thread.quit()
        self.progress_dlg.close()
        
        main_root = self.panels[0]['model'].rootPath()
        dlg = ConvergenceDialog(dups, main_root, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.action_type == 'trash':
                trashed_files = []
                errors = []
                for f in dlg.selected_files:
                    try:
                        send2trash(f)
                        trashed_files.append(f)
                    except Exception as e:
                        errors.append(f"{os.path.basename(f)}: {e}")
                if errors:
                    QMessageBox.warning(self, "部分失敗", "以下檔案無法移至回收筒：\n" + "\n".join(errors))
                if trashed_files:
                    # 只保護主目錄，其他來源資料夾可在使用者確認後清理
                    protected_roots = [main_root]
                    initial_empty, protected_set = self._collect_initial_empty_dirs(trashed_files, protected_roots)
                    if initial_empty:
                        ans = QMessageBox.question(
                            self,
                            "偵測到空資料夾",
                            f"移至回收筒後發現空資料夾。\n是否一併刪除這些空資料夾（含連帶變空的上層）？",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        if ans == QMessageBox.StandardButton.Yes:
                            saved = self._release_panel_watchers()
                            try:
                                deleted = self._delete_empty_dirs_cascade(initial_empty, protected_set)
                            finally:
                                self._restore_panel_watchers(saved)
                            QMessageBox.information(self, "完成", f"已移至回收筒，並刪除 {deleted} 個空資料夾。")
                        else:
                            QMessageBox.information(self, "完成", "已移至回收筒")
                    else:
                        QMessageBox.information(self, "完成", "已移至回收筒")
            elif dlg.action_type == 'move':
                target = QFileDialog.getExistingDirectory(self, "選取移動目標根目錄")
                if target:
                    all_roots = self._get_active_panel_roots()
                    pre = MovePreviewDialog(dlg.selected_files, all_roots, target, self)
                    if pre.exec() == QDialog.DialogCode.Accepted:
                        moved_files = []
                        errors = []
                        for f in dlg.selected_files:
                            try:
                                src_root = _find_source_root(f, all_roots)
                                rel = _safe_relpath(f, src_root)
                                dst = os.path.join(target, rel)
                                os.makedirs(os.path.dirname(dst), exist_ok=True)
                                shutil.move(f, dst)
                                moved_files.append(f)
                            except Exception as e:
                                errors.append(f"{os.path.basename(f)}: {e}")
                        if errors:
                            QMessageBox.warning(self, "部分失敗", "以下檔案無法移動：\n" + "\n".join(errors))

                        # 只保護主目錄，其他來源資料夾可在使用者確認後清理
                        protected_roots = [main_root]
                        initial_empty, protected_set = self._collect_initial_empty_dirs(moved_files, protected_roots)
                        if initial_empty:
                            ans = QMessageBox.question(
                                self,
                                "偵測到空資料夾",
                                f"移動完成後發現 {len(initial_empty)} 個空資料夾（含上層可能連帶變空的資料夾）。\n是否一併刪除這些空資料夾？",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No,
                            )
                            if ans == QMessageBox.StandardButton.Yes:
                                saved = self._release_panel_watchers()
                                try:
                                    deleted = self._delete_empty_dirs_cascade(initial_empty, protected_set)
                                finally:
                                    self._restore_panel_watchers(saved)
                                QMessageBox.information(self, "完成", f"鏡像移動作業結束，已刪除 {deleted} 個空資料夾（含連帶清理的上層空資料夾）。")
                            else:
                                QMessageBox.information(self, "完成", "鏡像移動作業結束")
                        else:
                            QMessageBox.information(self, "完成", "鏡像移動作業結束")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    w = MainWindow(); w.show(); sys.exit(app.exec())