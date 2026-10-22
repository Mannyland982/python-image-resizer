# Image Processor Pro

A multi-threaded, desktop batch image processing utility built with Python, Pillow, and Tkinter / ttkbootstrap. Designed around SOLID architecture principles for reliability, safe atomic writing, and clean separation of concerns.

![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Features

- **Concurrent Processing:** Multi-threaded worker pool utilizing all available CPU cores without freezing the UI.
- **Atomic File Writing:** Writes to temporary buffers before swapping to destination paths, preventing corrupted image files upon unexpected shutdowns.
- **Smart Resizing:** Box-constrained resizing with proportional aspect-ratio preservation or exact manual dimensions.
- **Format Conversion:** Convert between JPEG, PNG, WEBP, and BMP with fine-tuned quality parameters.
- **Watermark Layering:** Place semi-transparent branding logos on images with customizable opacity and 5 standard anchor positions.
- **Metadata Management:** Retain camera EXIF metadata on JPEG outputs.
- **Directory Mirroring:** Automatically mirrors and preserves nested subfolder hierarchies in output targets.
- **In-Place Overwrite Mode:** Optional direct replacement of original image files in source folders.

---

## 🚀 Getting Started

### Prerequisites

- Python 3.9 or higher installed on your machine.

### Installation

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/Mannyland982/python-image-resizer.git](https://github.com/Mannyland982/python-image-resizer.git)
   cd python-image-resizer
   
1 (Optional) Create a virtual environment:
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

2 Install dependencies:
pip install -r requirements.txt

3 Run the application:
python main.py
