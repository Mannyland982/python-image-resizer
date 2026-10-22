import os
import sys
import logging
import threading
import tempfile
import concurrent.futures
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
from PIL import Image, ImageTk, ImageEnhance, ImageOps

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# =============================================================================
# OPTIONAL MODERN THEME SUPPORT (ttkbootstrap fallback)
# =============================================================================
USE_BOOTSTRAP = False
MeterWidget = None
ToastWidget = None

try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import *
    from ttkbootstrap.widgets import Meter
    try:
        from ttkbootstrap.toast import ToastNotification
        ToastWidget = ToastNotification
    except ImportError:
        pass
    USE_BOOTSTRAP = True
    MeterWidget = Meter
except ImportError:
    logging.info("ttkbootstrap not found: using native dark theme fallback.")

EXT_MAP = {
    'JPEG': '.jpg',
    'PNG': '.png',
    'WEBP': '.webp',
    'BMP': '.bmp'
}

SUPPORTED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff')

WATERMARK_POSITIONS = [
    "Center",
    "Top Left",
    "Top Right",
    "Bottom Left",
    "Bottom Right"
]

# =============================================================================
# DATA STRUCTURES
# =============================================================================
@dataclass
class ProcessingConfig:
    target_format: str = "JPEG"
    quality: int = 85
    max_width: int = 1920
    max_height: int = 1080
    keep_aspect: bool = True
    keep_structure: bool = True
    keep_exif: bool = True
    to_grayscale: bool = False
    overwrite_existing: bool = True
    in_place_overwrite: bool = False
    watermark_path: str = ""
    watermark_position: str = "Bottom Right"
    watermark_opacity: float = 0.5
    watermark_scale_divisor: int = 6

# =============================================================================
# SINGLE RESPONSIBILITY: IMAGE MANIPULATION ENGINE
# =============================================================================
class ImageProcessor:
    @staticmethod
    def process_and_save(input_path: str, output_path: str, config: ProcessingConfig) -> None:
        """Processes a single image file with resizing, color conversion, watermark, and atomic save."""
        with Image.open(input_path) as raw_img:
            transposed_img = ImageOps.exif_transpose(raw_img)
            img = (transposed_img or raw_img).copy()
            original_exif = raw_img.info.get('exif')

        target_fmt = config.target_format.upper()

        # Dimension resizing
        w_max = max(10, int(config.max_width))
        h_max = max(10, int(config.max_height))
        if config.keep_aspect:
            img.thumbnail((w_max, h_max), Image.Resampling.LANCZOS)
        else:
            img = img.resize((w_max, h_max), Image.Resampling.LANCZOS)

        # Grayscale conversion
        if config.to_grayscale:
            img = img.convert('L')

        # Watermarking
        if config.watermark_path and os.path.isfile(config.watermark_path):
            img = ImageProcessor._apply_watermark(img, config)

        # Channel & Color profile compatibility
        if target_fmt in ('JPEG', 'BMP'):
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    background.paste(img, mask=img.split()[3])
                else:
                    background.paste(img.convert('RGB'))
                img = background
            elif img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
        elif target_fmt in ('PNG', 'WEBP'):
            if img.mode not in ('RGB', 'RGBA', 'L'):
                img = img.convert('RGBA')

        # Save arguments configuration
        save_args = {'format': target_fmt}
        if target_fmt in ('JPEG', 'WEBP'):
            save_args['quality'] = max(1, min(100, int(config.quality)))

        if config.keep_exif and target_fmt == 'JPEG' and original_exif:
            save_args['exif'] = original_exif

        # Atomic file write to avoid file corruption
        out_dir = os.path.dirname(output_path)
        os.makedirs(out_dir, exist_ok=True)
        temp_fd, temp_file_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
        os.close(temp_fd)

        try:
            img.save(temp_file_path, **save_args)
            os.replace(temp_file_path, output_path)
        except Exception:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _apply_watermark(base_img: Image.Image, config: ProcessingConfig) -> Image.Image:
        """Applies a semi-transparent watermark logo to the image."""
        try:
            with Image.open(config.watermark_path) as wm_raw:
                wm = wm_raw.convert("RGBA")
                alpha = wm.split()[3]
                alpha = ImageEnhance.Brightness(alpha).enhance(float(config.watermark_opacity))
                wm.putalpha(alpha)

                scale_div = max(2, int(config.watermark_scale_divisor))
                target_w = max(20, base_img.width // scale_div)
                target_h = max(20, base_img.height // scale_div)
                wm.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)

                idx = (WATERMARK_POSITIONS.index(config.watermark_position) 
                       if config.watermark_position in WATERMARK_POSITIONS else 4)

                coords = [
                    ((base_img.width - wm.width) // 2, (base_img.height - wm.height) // 2),  # Center
                    (15, 15),                                                                  # Top Left
                    (base_img.width - wm.width - 15, 15),                                      # Top Right
                    (15, base_img.height - wm.height - 15),                                    # Bottom Left
                    (base_img.width - wm.width - 15, base_img.height - wm.height - 15)         # Bottom Right
                ]
                pos = coords[idx]

                temp = base_img.convert("RGBA")
                temp.paste(wm, pos, wm)

                if base_img.mode in ('RGB', 'L'):
                    return temp.convert(base_img.mode)
                return temp
        except Exception as e:
            logging.warning(f"Watermark rendering skipped due to error: {e}")
            return base_img

# =============================================================================
# CONCURRENT BATCH WORKER
# =============================================================================
class BatchService:
    def __init__(self, config: ProcessingConfig, log_callback, progress_callback):
        self.config = config
        self.log = log_callback
        self.progress = progress_callback
        self._lock = threading.Lock()
        self.processed = 0
        self.success_count = 0
        self.skip_count = 0
        self.error_count = 0

    def _process_single_file(self, file_path: str, src_root: str, dest_base: str, total_files: int):
        out_ext = EXT_MAP.get(self.config.target_format.upper(), '.jpg')
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        if self.config.in_place_overwrite:
            target_dir = os.path.dirname(file_path)
            dest_path = os.path.join(target_dir, f"{base_name}{out_ext}")
        else:
            rel_dir = os.path.relpath(os.path.dirname(file_path), src_root)
            target_dir = os.path.join(dest_base, rel_dir) if self.config.keep_structure else dest_base
            dest_path = os.path.join(target_dir, f"{base_name}{out_ext}")

        is_same_file = os.path.abspath(file_path) == os.path.abspath(dest_path)
        dest_exists = os.path.exists(dest_path)

        if not self.config.overwrite_existing and not self.config.in_place_overwrite:
            if dest_exists and not is_same_file:
                with self._lock:
                    self.skip_count += 1
                    self.processed += 1
                    current = self.processed
                self.log("SKIP", f"Skipped (already exists): {os.path.basename(dest_path)}")
                self.progress(current, total_files)
                return

        try:
            ImageProcessor.process_and_save(file_path, dest_path, self.config)
            with self._lock:
                self.success_count += 1
                self.processed += 1
                current = self.processed

            msg = (f"Overwritten: {os.path.basename(dest_path)}" 
                   if (dest_exists and (self.config.overwrite_existing or is_same_file)) 
                   else f"Saved: {os.path.basename(dest_path)}")
            self.log("SUCCESS", msg)
        except Exception as e:
            with self._lock:
                self.error_count += 1
                self.processed += 1
                current = self.processed
            self.log("ERROR", f"Error on {base_name}: {e}")

        self.progress(current, total_files)

    def run(self, task_list: List[Tuple[str, str]], dest_base: str):
        total = len(task_list)
        max_workers = min(8, max(2, os.cpu_count() or 4))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._process_single_file, f_path, src_root, dest_base, total)
                for f_path, src_root in task_list
            ]
            concurrent.futures.wait(futures)

        self.log(
            "SUMMARY", 
            f"Finished! Processed: {self.success_count} | Skipped: {self.skip_count} | Errors: {self.error_count}"
        )

# =============================================================================
# USER INTERFACE (DESKTOP GUI)
# =============================================================================
class ImageProcessorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.sources: List[str] = []

        self._configure_window_and_theme()
        self._build_interface()

    def _configure_window_and_theme(self):
        self.root.title("Image Processor Pro")
        self.root.geometry("1180x880")
        self.root.minsize(1000, 700)

        if not USE_BOOTSTRAP:
            self.root.configure(bg="#1c1d21")
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure(".", background="#1c1d21", foreground="#e6e6e6", font=("Segoe UI", 9))
            style.configure("TFrame", background="#1c1d21")
            style.configure("TLabelframe", background="#1c1d21", foreground="#ffffff")
            style.configure("TLabelframe.Label", background="#1c1d21", foreground="#00bc8c", font=("Segoe UI", 10, "bold"))
            style.configure("TButton", background="#2b2d35", foreground="#ffffff", borderwidth=1, padding=6)
            style.map("TButton", background=[("active", "#3b3e4a")])
            style.configure("Accent.TButton", background="#00bc8c", foreground="#ffffff", font=("Segoe UI", 11, "bold"))
            style.map("Accent.TButton", background=[("active", "#009970")])
            style.configure("TCheckbutton", background="#1c1d21", foreground="#e6e6e6")
            style.configure("TLabel", background="#1c1d21", foreground="#e6e6e6")
            style.configure("TNotebook", background="#1c1d21", borderwidth=0)
            style.configure("TNotebook.Tab", background="#2b2d35", foreground="#ffffff", padding=[12, 5])
            style.map("TNotebook.Tab", background=[("selected", "#00bc8c")], foreground=[("selected", "#ffffff")])
            style.configure("Treeview", background="#141518", foreground="#e6e6e6", fieldbackground="#141518", rowheight=26)
            style.map("Treeview", background=[("selected", "#375a7f")])

    def _build_interface(self):
        main_box = ttk.Frame(self.root, padding=16)
        main_box.pack(fill=tk.BOTH, expand=True)

        # Header
        header = ttk.Frame(main_box)
        header.pack(fill=tk.X, pady=(0, 12))
        lbl_title = ttk.Label(header, text="Image Processor", font=("Segoe UI", 18, "bold"))
        lbl_title.pack(side=tk.LEFT)
        if not USE_BOOTSTRAP:
            lbl_title.configure(foreground="#ffffff")

        # Two-Column Layout
        body = ttk.Frame(main_box)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=6, minsize=520)
        body.columnconfigure(1, weight=5, minsize=460)
        body.rowconfigure(0, weight=1)

        left_col = ttk.Frame(body)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        # 1. Source Folders Section
        s1 = ttk.LabelFrame(left_col, text=" 📂 1. Source Directories ", padding=10)
        s1.pack(fill=tk.X, pady=(0, 8))

        tree_box = ttk.Frame(s1)
        tree_box.pack(fill=tk.X, pady=2)
        self.tree_sources = ttk.Treeview(tree_box, columns=("path",), show="headings", height=4)
        self.tree_sources.heading("path", text="Selected Directories Scanned Recursively")
        self.tree_sources.pack(side=tk.LEFT, fill=tk.X, expand=True)
        scrl = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree_sources.yview)
        self.tree_sources.configure(yscrollcommand=scrl.set)
        scrl.pack(side=tk.RIGHT, fill=tk.Y)

        s1_btns = ttk.Frame(s1)
        s1_btns.pack(fill=tk.X, pady=(8, 2))
        ttk.Button(s1_btns, text="+ Add Folder", command=self._add_directory).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(s1_btns, text="- Remove Folder", command=self._remove_directory).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(s1_btns, text="Clear All", command=self._clear_directories).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # 2. Options Notebook Section
        s2 = ttk.LabelFrame(left_col, text=" ⚙️ 2. Processing Parameters ", padding=10)
        s2.pack(fill=tk.X, pady=6)

        nb = ttk.Notebook(s2)
        nb.pack(fill=tk.X, pady=4)

        # Tab: Resolution & Format
        tab_dim = ttk.Frame(nb, padding=12)
        nb.add(tab_dim, text=" Format & Resolution ")

        ttk.Label(tab_dim, text="Output Format:").grid(row=0, column=0, sticky="w", pady=4)
        self.val_fmt = tk.StringVar(value="JPEG")
        ttk.Combobox(tab_dim, textvariable=self.val_fmt, values=["JPEG", "PNG", "WEBP", "BMP"], state="readonly", width=12).grid(row=0, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(tab_dim, text="Quality (JPEG/WEBP):").grid(row=1, column=0, sticky="w", pady=4)
        self.val_qual = tk.IntVar(value=85)
        ttk.Scale(tab_dim, from_=10, to=100, variable=self.val_qual, orient="horizontal").grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        lbl_q_val = ttk.Label(tab_dim, text="85%")
        lbl_q_val.grid(row=1, column=2, sticky="w", padx=4)
        self.val_qual.trace_add("write", lambda *_: lbl_q_val.config(text=f"{self.val_qual.get()}%"))

        ttk.Label(tab_dim, text="Bounding Box:").grid(row=2, column=0, sticky="w", pady=6)
        res_f = ttk.Frame(tab_dim)
        res_f.grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=6)
        self.val_w = tk.IntVar(value=1920)
        self.val_h = tk.IntVar(value=1080)
        ttk.Entry(res_f, textvariable=self.val_w, width=6).pack(side=tk.LEFT)
        ttk.Label(res_f, text=" × ").pack(side=tk.LEFT)
        ttk.Entry(res_f, textvariable=self.val_h, width=6).pack(side=tk.LEFT)
        ttk.Label(res_f, text=" px").pack(side=tk.LEFT)

        # Tab: Advanced Settings
        tab_adv = ttk.Frame(nb, padding=12)
        nb.add(tab_adv, text=" Output Settings ")

        self.val_aspect = tk.BooleanVar(value=True)
        self.val_struct = tk.BooleanVar(value=True)
        self.val_exif = tk.BooleanVar(value=True)
        self.val_gray = tk.BooleanVar(value=False)
        self.val_ovr = tk.BooleanVar(value=True)
        self.val_inplace = tk.BooleanVar(value=False)

        ttk.Checkbutton(tab_adv, text="Preserve Aspect Ratio (Proportional)", variable=self.val_aspect).pack(anchor="w", pady=2)
        ttk.Checkbutton(tab_adv, text="Retain EXIF Camera Metadata (JPEG only)", variable=self.val_exif).pack(anchor="w", pady=2)
        ttk.Checkbutton(tab_adv, text="Preserve Subfolder Hierarchy (Mirroring)", variable=self.val_struct).pack(anchor="w", pady=2)
        ttk.Checkbutton(tab_adv, text="Convert to Grayscale (B&W)", variable=self.val_gray).pack(anchor="w", pady=2)
        ttk.Separator(tab_adv, orient="horizontal").pack(fill=tk.X, pady=6)
        ttk.Checkbutton(tab_adv, text="Overwrite Target Files with Same Name", variable=self.val_ovr).pack(anchor="w", pady=2)
        ttk.Checkbutton(tab_adv, text="In-Place Direct Overwrite (Replaces original files)", variable=self.val_inplace, command=self._toggle_inplace).pack(anchor="w", pady=2)

        # Tab: Watermark
        tab_wm = ttk.Frame(nb, padding=12)
        nb.add(tab_wm, text=" Watermark ")

        self.val_wm_path = tk.StringVar(value="")
        wm_file_box = ttk.Frame(tab_wm)
        wm_file_box.pack(fill=tk.X, pady=3)
        ttk.Entry(wm_file_box, textvariable=self.val_wm_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(wm_file_box, text="Browse...", command=self._pick_watermark).pack(side=tk.LEFT, padx=6)

        wm_sub = ttk.Frame(tab_wm)
        wm_sub.pack(fill=tk.X, pady=6)
        ttk.Label(wm_sub, text="Position:").grid(row=0, column=0, sticky="w", pady=3)
        self.val_wm_pos = tk.StringVar(value="Bottom Right")
        ttk.Combobox(wm_sub, textvariable=self.val_wm_pos, values=WATERMARK_POSITIONS, state="readonly").grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(wm_sub, text="Opacity:").grid(row=1, column=0, sticky="w", pady=3)
        self.val_wm_opac = tk.DoubleVar(value=0.5)
        ttk.Scale(wm_sub, from_=0.1, to=1.0, variable=self.val_wm_opac, orient="horizontal").grid(row=1, column=1, sticky="ew", padx=6, pady=3)

        # 3. Output Destination Section
        self.s3 = ttk.LabelFrame(left_col, text=" 💾 3. Destination Directory ", padding=10)
        self.s3.pack(fill=tk.X, pady=6)

        self.val_dest = tk.StringVar(value="")
        f_dest = ttk.Frame(self.s3)
        f_dest.pack(fill=tk.X, pady=2)
        self.entry_dest = ttk.Entry(f_dest, textvariable=self.val_dest)
        self.entry_dest.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_dest = ttk.Button(f_dest, text="Browse...", command=self._pick_destination)
        self.btn_dest.pack(side=tk.LEFT, padx=(6, 0))

        # Run Button
        run_style = "Accent.TButton" if not USE_BOOTSTRAP else None
        self.btn_run = ttk.Button(left_col, text="🚀 START BATCH PROCESSING", command=self._start_processing, style=run_style)
        if USE_BOOTSTRAP:
            self.btn_run.configure(bootstyle="success")
        self.btn_run.pack(fill=tk.X, ipady=12, pady=(12, 0))

        # Right Column: Monitoring & Logs
        right_col = ttk.Frame(body)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right_col.rowconfigure(1, weight=1)
        right_col.columnconfigure(0, weight=1)

        dash_box = ttk.LabelFrame(right_col, text=" 📊 Execution Status ", padding=10)
        dash_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        if MeterWidget and USE_BOOTSTRAP:
            self.meter = MeterWidget(dash_box, metersize=155, amountused=0, metertype="doughnut", bootstyle="success", textright="%")
            self.meter.pack(pady=2)
        else:
            self.meter = None
            self.prog_bar = ttk.Progressbar(dash_box, orient="horizontal", mode="determinate")
            self.prog_bar.pack(fill=tk.X, pady=6, padx=4)

        self.lbl_stats = ttk.Label(dash_box, text="Idle - awaiting job configuration", font=("Segoe UI", 9))
        self.lbl_stats.pack(pady=2)

        log_box = ttk.LabelFrame(right_col, text=" 📝 Activity Log ", padding=8)
        log_box.grid(row=1, column=0, sticky="nsew")

        self.log_area = scrolledtext.ScrolledText(
            log_box, font=('Consolas', 9), bg="#101114", fg="#cccccc",
            insertbackground="#ffffff", selectbackground="#375a7f",
            relief="flat", state='disabled'
        )
        self.log_area.pack(fill=tk.BOTH, expand=True)

        self.log_area.tag_config('SUCCESS', foreground='#00bc8c', font=('Consolas', 9, 'bold'))
        self.log_area.tag_config('ERROR', foreground='#e74c3c', font=('Consolas', 9, 'bold'))
        self.log_area.tag_config('SKIP', foreground='#f39c12')
        self.log_area.tag_config('INFO', foreground='#3498db')
        self.log_area.tag_config('SUMMARY', foreground='#ffffff', font=('Consolas', 9, 'bold'))

        # Footer
        footer = ttk.Frame(main_box, padding=(0, 10, 0, 0))
        footer.pack(fill=tk.X)
        lbl_foot = ttk.Label(footer, text="Open Source Batch Image Processor • Built with Python & Pillow", font=("Segoe UI", 8), anchor="center")
        lbl_foot.pack(fill=tk.X)
        if not USE_BOOTSTRAP:
            lbl_foot.configure(foreground="#888899")

    def _toggle_inplace(self):
        if self.val_inplace.get():
            self.entry_dest.config(state='disabled')
            self.btn_dest.config(state='disabled')
            self.s3.config(text=" 💾 3. Destination: In-Place Mode Active ")
        else:
            self.entry_dest.config(state='normal')
            self.btn_dest.config(state='normal')
            self.s3.config(text=" 💾 3. Destination Directory ")

    def _add_directory(self):
        dir_selected = filedialog.askdirectory()
        if dir_selected and dir_selected not in self.sources:
            self.sources.append(dir_selected)
            self.tree_sources.insert("", tk.END, values=(dir_selected,))

    def _remove_directory(self):
        for item in self.tree_sources.selection():
            val = self.tree_sources.item(item, "values")[0]
            if val in self.sources:
                self.sources.remove(val)
            self.tree_sources.delete(item)

    def _clear_directories(self):
        self.sources.clear()
        for row in self.tree_sources.get_children():
            self.tree_sources.delete(row)

    def _pick_destination(self):
        dir_selected = filedialog.askdirectory()
        if dir_selected:
            self.val_dest.set(dir_selected)

    def _pick_watermark(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image files", "*.png;*.webp;*.jpg;*.jpeg;*.bmp")])
        if file_path:
            self.val_wm_path.set(file_path)

    def _append_log(self, level: str, message: str):
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, f"[{level}] ", level)
        self.log_area.insert(tk.END, f"{message}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    def _update_progress(self, current: int, total: int):
        pct = int((current / total) * 100) if total > 0 else 0
        self.lbl_stats.config(text=f"Progress: {current} / {total} files ({pct}%)")

        if self.meter:
            self.meter.configure(amountused=pct)
        elif hasattr(self, 'prog_bar'):
            self.prog_bar.config(value=pct)

    def _start_processing(self):
        dest = self.val_dest.get().strip()
        in_place = self.val_inplace.get()

        if not self.sources:
            messagebox.showwarning("Missing Input", "Please select at least one source directory.")
            return

        if not in_place and not dest:
            messagebox.showwarning("Missing Destination", "Please choose a valid destination folder or select in-place processing.")
            return

        if in_place:
            proceed = messagebox.askyesno(
                "Confirm In-Place Overwrite",
                "You have selected in-place processing.\nOriginal source images will be permanently modified or overwritten.\n\nContinue?"
            )
            if not proceed:
                return

        # Discovery phase
        tasks = []
        for src in self.sources:
            for root_dir, _, files in os.walk(src):
                for name in files:
                    if name.lower().endswith(SUPPORTED_EXTENSIONS):
                        tasks.append((os.path.join(root_dir, name), src))

        if not tasks:
            messagebox.showinfo("No Files", "No compatible image files were found in the selected directories.")
            return

        config = ProcessingConfig(
            target_format=self.val_fmt.get(),
            quality=self.val_qual.get(),
            max_width=self.val_w.get(),
            max_height=self.val_h.get(),
            keep_aspect=self.val_aspect.get(),
            keep_structure=self.val_struct.get(),
            keep_exif=self.val_exif.get(),
            to_grayscale=self.val_gray.get(),
            overwrite_existing=self.val_ovr.get(),
            in_place_overwrite=in_place,
            watermark_path=self.val_wm_path.get(),
            watermark_position=self.val_wm_pos.get(),
            watermark_opacity=self.val_wm_opac.get()
        )

        self.btn_run.config(state='disabled')
        self._append_log("INFO", f"Starting batch processing of {len(tasks)} files across multiple worker threads...")

        batch_worker = BatchService(
            config,
            lambda lvl, msg: self.root.after(0, lambda: self._append_log(lvl, msg)),
            lambda cur, tot: self.root.after(0, lambda: self._update_progress(cur, tot))
        )

        def run_thread():
            batch_worker.run(tasks, dest)
            self.root.after(0, lambda: self.btn_run.config(state='normal'))
            if ToastWidget and USE_BOOTSTRAP:
                self.root.after(0, lambda: ToastWidget(
                    title="Batch Complete",
                    message=f"{len(tasks)} images processed successfully.",
                    bootstyle="success"
                ).show_toast())

        threading.Thread(target=run_thread, daemon=True).start()

# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    if USE_BOOTSTRAP:
        root_window = tb.Window(themename="darkly")
    else:
        root_window = tk.Tk()

    app = ImageProcessorApp(root_window)
    root_window.mainloop()
