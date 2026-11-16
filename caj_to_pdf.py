#!/usr/bin/env python3
"""Convert CNKI .caj journal files to PDF, one file or a whole folder.

Works offline. Uses only the Python standard library (no pip packages).

A .caj file is not one single format. The first bytes say which kind it is:

  CAJ     The pages are already PDF objects, with the catalog and xref table
          left out. This script puts those pieces back and writes a PDF.
  %PDF    The file is already a PDF that was given a .caj name. It is copied.
  KDH     The PDF bytes are masked. The mask is removed, then the PDF is saved.
  HN/C8   Each page is a JPEG image. Those images are placed on PDF pages.
          JBIG scans and text-only HN files are reported and skipped.

Run with no arguments to open a window, or:

    python caj_to_pdf.py INPUT_FOLDER OUTPUT_FOLDER
    python caj_to_pdf.py --recursive -o C:\\pdfs C:\\caj
    python caj_to_pdf.py -o C:\\pdfs a.caj b.caj
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import threading
from pathlib import Path


# KDH stores the PDF under a fixed byte mask that belongs to the file format.
_KDH_MASK = b"FZHMEI"
_PDF_OBJ_HEAD = re.compile(rb"(\d+)\s+0\s+obj\b")


class CajFormatError(Exception):
    """The file is not a .caj document this tool can turn into a PDF."""


def _caj_kind(data):
    if data.startswith(b"%PDF") or data[:64].find(b"%PDF") != -1:
        return "PDF"
    if data[:1] == b"\xc8":
        return "C8"
    if data[:2] == b"HN":
        return "HN"
    head = data[:4].replace(b"\x00", b"")
    if head.startswith(b"CAJ"):
        return "CAJ"
    if head.startswith(b"KDH"):
        return "KDH"
    if head.startswith(b"TEB"):
        return "TEB"
    return None


def _object_end(chunk):
    """Byte length of one PDF object, using /Length when a stream declares it."""
    match = _PDF_OBJ_HEAD.match(chunk)
    if match is None:
        return None
    pos = match.end()
    stream_at = chunk.find(b"stream", pos)
    endobj_at = chunk.find(b"endobj", pos)
    if stream_at != -1 and (endobj_at == -1 or stream_at < endobj_at):
        length_match = re.search(rb"/Length\s+(\d+)\b", chunk[pos:stream_at])
        data_at = stream_at + len(b"stream")
        if chunk[data_at:data_at + 2] == b"\r\n":
            data_at += 2
        elif chunk[data_at:data_at + 1] in (b"\n", b"\r"):
            data_at += 1
        if length_match is not None:
            endobj_at = chunk.find(b"endobj", data_at + int(length_match.group(1)))
        else:
            endstream_at = chunk.find(b"endstream", data_at)
            if endstream_at != -1:
                endobj_at = chunk.find(b"endobj", endstream_at)
    if endobj_at == -1:
        return None
    return endobj_at + len(b"endobj")


def _split_pdf_objects(data):
    heads = list(_PDF_OBJ_HEAD.finditer(data))
    objects = []
    for index, match in enumerate(heads):
        start = match.start()
        region_end = heads[index + 1].start() if index + 1 < len(heads) else len(data)
        end = _object_end(data[start:region_end])
        if end is None:
            end = _object_end(data[start:])
        if end is None:
            continue
        objects.append((int(match.group(1)), data[start:start + end]))
    return objects


def _append_xref(buffer, offsets, root):
    max_id = max(offsets) if offsets else 0
    xref_at = len(buffer)
    buffer += f"xref\n0 {max_id + 1}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for number in range(1, max_id + 1):
        if number in offsets:
            buffer += f"{offsets[number]:010d} 00000 n \n".encode("ascii")
        else:
            buffer += b"0000000000 00000 f \n"
    buffer += (
        f"trailer\n<< /Size {max_id + 1} /Root {root} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(buffer)


def _write_pdf_objects(objects, root):
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += objects[number]
        if not objects[number].endswith(b"\n"):
            out += b"\n"
    return _append_xref(out, offsets, root)


def _rebuild_pdf(body, page_count):
    """Add the catalog and the xref table that a CAJ body leaves out."""
    if body.startswith(b"%PDF") and b"startxref" in body and b"%%EOF" in body:
        eof = body.rfind(b"%%EOF")
        return body[:eof + 5] + b"\n"
    parsed = _split_pdf_objects(body)
    if not parsed:
        raise CajFormatError("The CAJ file did not contain PDF page objects.")
    obj_map = {}
    order = []
    for number, chunk in parsed:
        if number not in obj_map:
            obj_map[number] = chunk
            order.append(number)
    catalog_no = None
    for number in order:
        if re.search(rb"/Type\s*/Catalog\b", obj_map[number]):
            catalog_no = number
            break
    if catalog_no is None:
        parent_refs = []
        for number in order:
            for match in re.finditer(rb"/Parent\s+(\d+)\s+0\s+R", obj_map[number]):
                parent_refs.append((number, int(match.group(1))))
        if not parent_refs:
            raise CajFormatError("The PDF inside this CAJ file has no pages.")
        missing = []
        for _child, parent in parent_refs:
            if parent not in obj_map and parent not in missing:
                missing.append(parent)
        root_pages = min(parent for _child, parent in parent_refs)
        for parent in missing:
            kids = []
            seen = set()
            for child, pref in parent_refs:
                if pref == parent and child not in seen:
                    seen.add(child)
                    kids.append(child)
            count = len(kids)
            if parent == root_pages and page_count >= count and page_count > 0:
                count = page_count
            kid_text = " ".join(f"{kid} 0 R" for kid in kids)
            obj_map[parent] = (
                f"{parent} 0 obj\n<< /Type /Pages /Kids [{kid_text}] /Count {count} >>\nendobj"
            ).encode("ascii")
            order.append(parent)
        catalog_no = max(obj_map) + 1
        obj_map[catalog_no] = (
            f"{catalog_no} 0 obj\n<< /Type /Catalog /Pages {root_pages} 0 R >>\nendobj"
        ).encode("ascii")
        order.append(catalog_no)
    return _write_pdf_objects({number: obj_map[number] for number in order}, catalog_no)


def _pdf_slice(data):
    start = data.find(b"%PDF")
    if start < 0:
        raise CajFormatError("No PDF was found in this file.")
    eof = data.rfind(b"%%EOF")
    if eof == -1 or eof < start:
        return _rebuild_pdf(data[start:], 0)
    return data[start:eof + 5] + b"\n"


def _caj_embedded_pdf(data):
    if len(data) < 0x18:
        raise CajFormatError("This CAJ file is too small to contain a document.")
    page_count = struct.unpack_from("<i", data, 0x10)[0]
    if page_count < 0 or page_count > 20000:
        page_count = 0
    pointer = struct.unpack_from("<i", data, 0x14)[0]
    if pointer < 0 or pointer > len(data) - 4:
        raise CajFormatError("This CAJ file does not point at a PDF body.")
    pdf_start = struct.unpack_from("<i", data, pointer)[0]
    if pdf_start < 0 or pdf_start >= len(data):
        raise CajFormatError("This CAJ file does not point at a PDF body.")
    last = data.rfind(b"endobj", pdf_start)
    if last == -1:
        raise CajFormatError("This CAJ file has no PDF objects.")
    body = data[pdf_start:last + len(b"endobj")]
    return page_count, _rebuild_pdf(body, page_count)


def _unwrap_kdh(data):
    if len(data) <= 254:
        raise CajFormatError("This KDH file is too small to contain a PDF.")
    masked = data[254:]
    plain = bytearray(len(masked))
    for index, value in enumerate(masked):
        plain[index] = value ^ _KDH_MASK[index % len(_KDH_MASK)]
    return _pdf_slice(bytes(plain))


def _jpeg_size(blob):
    if len(blob) < 4 or blob[0:2] != b"\xff\xd8":
        return None
    index = 2
    limit = len(blob)
    while index + 8 < limit:
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width, components = struct.unpack_from(">HHB", blob, index + 5)
            if width > 0 and height > 0:
                return width, height, components
            return None
        if marker == 0xD9:
            return None
        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD8:
            index += 2
            continue
        if index + 4 > limit:
            return None
        length = struct.unpack_from(">H", blob, index + 2)[0]
        if length < 2:
            return None
        index += 2 + length
    return None


def _hn_page_table(data):
    if data[:2] == b"HN" and len(data) >= 6 and data[4:6] == b"\xc8\x00":
        return 0x90, 0xD8
    if data[:1] == b"\xc8":
        return 0x08, 0x50
    toc_at = 0x158
    if len(data) < toc_at + 4:
        raise CajFormatError("This HN file is too small to contain pages.")
    toc_count = struct.unpack_from("<i", data, toc_at)[0]
    if toc_count < 0 or toc_count > 5000:
        raise CajFormatError("This HN file has an unreadable table of contents.")
    return 0x90, toc_at + 4 + 0x134 * toc_count


def _hn_to_pdf(data):
    page_at, info_at = _hn_page_table(data)
    if page_at + 4 > len(data):
        raise CajFormatError("This HN file is too small to contain pages.")
    page_count = struct.unpack_from("<i", data, page_at)[0]
    if page_count <= 0 or page_count > 5000:
        raise CajFormatError("This HN file does not contain a readable page count.")
    images = []
    blocked = None
    for index in range(page_count):
        base = info_at + index * 20
        if base + 20 > len(data):
            raise CajFormatError("This HN file ends before all of its pages.")
        page_off, text_size, image_count, _page_no, _unk, _next = struct.unpack_from("<iihhii", data, base)
        if page_off < 0 or text_size < 0 or image_count < 0 or image_count > 500:
            raise CajFormatError("A page record in this HN file is not usable.")
        cursor = page_off + text_size
        if cursor < 0 or cursor > len(data):
            raise CajFormatError("A page record in this HN file points outside the file.")
        for _image_index in range(image_count):
            if cursor + 12 > len(data):
                raise CajFormatError("An image block in this HN file is incomplete.")
            image_type, data_off, data_size = struct.unpack_from("<iii", data, cursor)
            if data_size < 0 or data_off < 0 or data_off + data_size > len(data):
                raise CajFormatError("An image block in this HN file points outside the file.")
            blob = data[data_off:data_off + data_size]
            cursor = data_off + data_size
            if image_type in (0, 3):
                blocked = "JBIG" if image_type == 0 else "JBIG2"
                continue
            if image_type not in (1, 2):
                raise CajFormatError(
                    "This HN file uses an image type this offline script cannot place in a PDF."
                )
            size = _jpeg_size(blob)
            if size is None:
                raise CajFormatError("A page was marked as JPEG but the image is not a JPEG.")
            width, height, components = size
            images.append((blob, width, height, components, image_type == 1))
    if blocked and not images:
        raise CajFormatError(
            "This .caj file is an HN scan and its pages are "
            + blocked
            + " images. Plain Python has no JBIG decoder, so those pages cannot be rebuilt offline. "
            "Open the file in CAJViewer and export or print it to PDF."
        )
    if not images:
        raise CajFormatError(
            "This .caj file stores the article as HN text rather than as PDF pages or JPEG scans. "
            "Open it in CAJViewer and export or print it to PDF."
        )
    return _jpeg_pages_pdf(images), len(images), blocked


def _jpeg_pages_pdf(images):
    objects = {}
    kids = []
    number = 3
    for blob, width, height, components, flip in images:
        content_no = number
        image_no = number + 1
        page_no = number + 2
        number += 3
        width = abs(int(width))
        height = abs(int(height))
        if flip:
            commands = f"q\n{width} 0 0 {-height} 0 {height} cm\n/Im Do\nQ\n"
        else:
            commands = f"q\n{width} 0 0 {height} 0 0 cm\n/Im Do\nQ\n"
        stream = commands.encode("ascii")
        objects[content_no] = (
            f"{content_no} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream\nendobj"
        )
        if components == 1:
            color = "/DeviceGray"
        elif components == 4:
            color = "/DeviceCMYK"
        else:
            color = "/DeviceRGB"
        objects[image_no] = (
            f"{image_no} 0 obj\n<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace {color} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(blob)} >>\nstream\n".encode("ascii")
            + blob
            + b"\nendstream\nendobj"
        )
        objects[page_no] = (
            f"{page_no} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Contents {content_no} 0 R /Resources << /XObject << /Im {image_no} 0 R >> >> >>\nendobj"
        ).encode("ascii")
        kids.append(page_no)
    kid_text = " ".join(f"{kid} 0 R" for kid in kids)
    objects[2] = (
        f"2 0 obj\n<< /Type /Pages /Kids [{kid_text}] /Count {len(kids)} >>\nendobj"
    ).encode("ascii")
    objects[1] = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj"
    return _write_pdf_objects(objects, root=1)


def convert_caj_file(src, dest):
    data = Path(src).read_bytes()
    if not data:
        raise CajFormatError("The file is empty.")
    kind = _caj_kind(data)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "PDF":
        dest.write_bytes(_pdf_slice(data))
        return "file was already a PDF"
    if kind == "CAJ":
        page_count, pdf = _caj_embedded_pdf(data)
        dest.write_bytes(pdf)
        if page_count:
            return f"embedded PDF restored, {page_count} pages"
        return "embedded PDF restored"
    if kind == "KDH":
        dest.write_bytes(_unwrap_kdh(data))
        return "KDH mask removed"
    if kind in {"HN", "C8"}:
        pdf, image_pages, blocked = _hn_to_pdf(data)
        dest.write_bytes(pdf)
        if blocked:
            return f"{image_pages} JPEG pages; {blocked} pages were left out"
        return f"{image_pages} JPEG pages"
    if kind == "TEB":
        raise CajFormatError(
            "This is a TEB journal file. Open it in CAJViewer and export or print it to PDF."
        )
    raise CajFormatError(
        "This file is not a CAJ journal document. The script reads an embedded PDF, "
        "a PDF saved with the .caj extension, a KDH file, or an HN file whose pages are JPEG."
    )


def _unique_pdf(folder, stem, used):
    name = stem + ".pdf"
    if name.lower() not in used:
        used.add(name.lower())
        return folder / name
    number = 2
    while f"{stem}_{number}.pdf".lower() in used:
        number += 1
    name = f"{stem}_{number}.pdf"
    used.add(name.lower())
    return folder / name


def convert_files(paths, output_dir, log=print):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    ok = 0
    failed = 0
    for src in paths:
        src = Path(src)
        dest = _unique_pdf(output_dir, src.stem, used)
        try:
            summary = convert_caj_file(src, dest)
            log(f"OK    {src.name}  ->  {dest.name}    ({summary})")
            ok += 1
        except CajFormatError as exc:
            log(f"SKIP  {src.name}: {exc}")
            failed += 1
        except Exception as exc:
            log(f"FAIL  {src.name}: {exc}")
            failed += 1
    log(f"Done. {ok} PDF file(s) written, {failed} not converted. Output: {output_dir}")
    return ok, failed


def collect_caj_files(inputs, recursive=False):
    found = []
    missing = []
    for item in inputs:
        path = Path(item)
        if not path.exists():
            missing.append(str(path))
            continue
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            for candidate in iterator:
                if candidate.is_file() and candidate.suffix.lower() == ".caj":
                    found.append(candidate)
        else:
            found.append(path)
    unique = []
    seen = set()
    for path in found:
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    unique.sort(key=lambda item: str(item).lower())
    return unique, missing


def _default_output(inputs):
    first = Path(inputs[0])
    parent = first if first.is_dir() else first.parent
    return parent / "pdf_out"


def launch_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        print("Could not open a window:", exc)
        print(r"Use:  python caj_to_pdf.py INPUT_FOLDER OUTPUT_FOLDER")
        return 1

    root = tk.Tk()
    root.title("CAJ to PDF")
    root.geometry("760x560")
    root.minsize(640, 460)

    intro = (
        "Select .caj files, or a folder of them, and write one PDF for each file.\n"
        "Offline. No extra packages. The PDF inside a normal CAJ file is restored. "
        "Page size stays as it was in the journal file."
    )
    ttk.Label(root, text=intro, justify="left", wraplength=720).pack(fill="x", padx=12, pady=(12, 6))

    files = []
    list_frame = ttk.Frame(root)
    list_frame.pack(fill="both", expand=True, padx=12, pady=4)
    scroll = ttk.Scrollbar(list_frame)
    scroll.pack(side="right", fill="y")
    box = tk.Listbox(list_frame, selectmode=tk.EXTENDED, yscrollcommand=scroll.set)
    box.pack(side="left", fill="both", expand=True)
    scroll.config(command=box.yview)

    def refresh():
        box.delete(0, tk.END)
        for path in files:
            box.insert(tk.END, str(path))

    def add_paths(new_paths):
        existing = {str(path).lower() for path in files}
        for path in new_paths:
            if str(path).lower() not in existing:
                files.append(path)
                existing.add(str(path).lower())
        refresh()

    recursive = tk.BooleanVar(value=False)

    def add_files():
        picked = filedialog.askopenfilenames(
            title="Select CAJ files",
            filetypes=[("CAJ", "*.caj"), ("All files", "*.*")],
        )
        add_paths([Path(item) for item in picked])

    def add_folder():
        folder = filedialog.askdirectory(title="Select a folder of CAJ files")
        if not folder:
            return
        found, _missing = collect_caj_files([folder], recursive=recursive.get())
        if not found:
            messagebox.showinfo("CAJ to PDF", "No .caj files were found in that folder.")
            return
        add_paths(found)
        if not output_var.get().strip():
            output_var.set(str(Path(folder) / "pdf_out"))

    def remove_selected():
        for index in reversed(box.curselection()):
            del files[index]
        refresh()

    def clear_files():
        files.clear()
        refresh()

    button_row = ttk.Frame(root)
    button_row.pack(fill="x", padx=12, pady=6)
    ttk.Button(button_row, text="Add files", command=add_files).pack(side="left")
    ttk.Button(button_row, text="Add folder", command=add_folder).pack(side="left", padx=(6, 0))
    ttk.Checkbutton(button_row, text="Include subfolders", variable=recursive).pack(side="left", padx=(10, 0))
    ttk.Button(button_row, text="Remove", command=remove_selected).pack(side="left", padx=(16, 0))
    ttk.Button(button_row, text="Clear", command=clear_files).pack(side="left", padx=(6, 0))

    options = ttk.Frame(root)
    options.pack(fill="x", padx=12, pady=4)
    ttk.Label(options, text="PDF folder").pack(side="left")
    output_var = tk.StringVar()
    ttk.Entry(options, textvariable=output_var).pack(side="left", fill="x", expand=True, padx=8)

    def browse_output():
        folder = filedialog.askdirectory(title="Select the PDF output folder")
        if folder:
            output_var.set(folder)

    ttk.Button(options, text="Browse", command=browse_output).pack(side="left")

    log_widget = tk.Text(root, height=12, wrap="word", state="disabled")
    log_widget.pack(fill="both", expand=False, padx=12, pady=(4, 8))

    def append_log(message):
        log_widget.configure(state="normal")
        log_widget.insert("end", message + "\n")
        log_widget.see("end")
        log_widget.configure(state="disabled")

    running = {"value": False}

    def on_convert():
        if running["value"]:
            return
        if not files:
            messagebox.showinfo("CAJ to PDF", "Add at least one .caj file or a folder first.")
            return
        target = output_var.get().strip()
        if not target:
            messagebox.showinfo("CAJ to PDF", "Choose a folder for the PDF files.")
            return
        running["value"] = True
        convert_button.configure(state="disabled")
        append_log(f"Converting {len(files)} file(s)...")

        def work():
            def log(message, captured=append_log):
                root.after(0, lambda text=message: captured(text))

            try:
                convert_files(list(files), target, log=log)
            finally:
                def finish():
                    running["value"] = False
                    convert_button.configure(state="normal")

                root.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    convert_button = ttk.Button(root, text="Convert all to PDF", command=on_convert)
    convert_button.pack(padx=12, pady=(0, 12), anchor="e")
    root.mainloop()
    return 0


def run_self_test():
    import tempfile

    def assert_openable_pdf(blob, label):
        if not blob.startswith(b"%PDF") or b"%%EOF" not in blob:
            raise AssertionError(f"{label} is not a PDF")
        xref_at = int(blob[blob.rfind(b"startxref"):].splitlines()[1])
        if blob[xref_at:xref_at + 4] != b"xref":
            raise AssertionError(f"{label} xref is at the wrong offset")
        if b"/Type /Catalog" not in blob and b"/Type/Catalog" not in blob:
            raise AssertionError(f"{label} has no catalog")

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        page = b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>\nendobj\n"
        caj = bytearray(0x84)
        caj[0:4] = b"CAJ\x00"
        struct.pack_into("<i", caj, 0x10, 1)
        struct.pack_into("<i", caj, 0x14, 0x80)
        struct.pack_into("<i", caj, 0x80, 0x84)
        caj.extend(page)
        (folder / "paper.caj").write_bytes(caj)
        summary = convert_caj_file(folder / "paper.caj", folder / "paper.pdf")
        rebuilt = (folder / "paper.pdf").read_bytes()
        assert_openable_pdf(rebuilt, "CAJ")
        if b"/Kids [3 0 R]" not in rebuilt or "1 pages" not in summary:
            raise AssertionError(summary)

        ready = _write_pdf_objects(
            {1: b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj"},
            root=1,
        )
        (folder / "already.caj").write_bytes(ready)
        if "already a PDF" not in convert_caj_file(folder / "already.caj", folder / "already.pdf"):
            raise AssertionError("PDF-shaped CAJ was not copied")

        masked = bytearray(b"KDH ")
        masked.extend(b"\x00" * (254 - 4))
        for index, value in enumerate(ready):
            masked.append(value ^ _KDH_MASK[index % len(_KDH_MASK)])
        (folder / "wrapped.caj").write_bytes(masked)
        if "KDH" not in convert_caj_file(folder / "wrapped.caj", folder / "wrapped.pdf"):
            raise AssertionError("KDH file was not recognized")
        assert_openable_pdf((folder / "wrapped.pdf").read_bytes(), "KDH")

        jpeg = b"\xff\xd8\xff\xc0" + struct.pack(">HBHHB", 11, 8, 2, 3, 1) + b"\x01\x11\x00\xff\xd9"
        hn = bytearray(0xEC + 12 + len(jpeg))
        hn[0:2] = b"HN"
        hn[4:6] = b"\xc8\x00"
        struct.pack_into("<i", hn, 0x90, 1)
        struct.pack_into("<iihhii", hn, 0xD8, 0xEC, 0, 1, 1, 0, 0)
        struct.pack_into("<iii", hn, 0xEC, 2, 0xEC + 12, len(jpeg))
        hn[0xEC + 12:] = jpeg
        (folder / "scan.caj").write_bytes(hn)
        hn_summary = convert_caj_file(folder / "scan.caj", folder / "scan.pdf")
        hn_pdf = (folder / "scan.pdf").read_bytes()
        assert_openable_pdf(hn_pdf, "HN")
        if b"/DCTDecode" not in hn_pdf or b"/Width 3" not in hn_pdf or "JPEG" not in hn_summary:
            raise AssertionError(hn_summary)

        jbig = bytearray(hn)
        struct.pack_into("<i", jbig, 0xEC, 0)
        (folder / "jbig.caj").write_bytes(jbig)
        try:
            convert_caj_file(folder / "jbig.caj", folder / "jbig.pdf")
        except CajFormatError as exc:
            if "JBIG" not in str(exc):
                raise AssertionError(exc) from exc
        else:
            raise AssertionError("JBIG CAJ was converted without a decoder")

    print("Self-test passed.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert CNKI .caj files to PDF in batch. Offline, no extra packages."
    )
    parser.add_argument("inputs", nargs="*", help=".caj files or folders")
    parser.add_argument("-o", "--output", help="Folder where PDF files are written")
    parser.add_argument("--recursive", action="store_true", help="Include .caj files in subfolders")
    parser.add_argument("--self-test", action="store_true", help="Check the converter and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.inputs:
        return launch_gui()

    files, missing = collect_caj_files(args.inputs, recursive=args.recursive)
    for item in missing:
        print(f"Not found: {item}")
    if not files:
        print("No .caj files found.")
        return 1
    output = Path(args.output) if args.output else _default_output(args.inputs)
    ok, failed = convert_files(files, output, log=print)
    if ok == 0 or failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
