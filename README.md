# CAJ to PDF

Convert CNKI `.caj` journal files to PDF, one file at a time or a whole folder.

The converter runs offline and uses only the Python standard library. No packages to install.

## Requirements

- Python 3
- On Windows, the window needs Tk, which ships with the standard Python installer

## Run

Double-click `run.bat`, or start the window from a terminal:

```
python caj_to_pdf.py
```

The window lets you add files or a folder, choose where the PDFs go, and convert them all. Check **Include subfolders** before adding a folder if the `.caj` files are nested.

From the command line:

```
python caj_to_pdf.py INPUT_FOLDER OUTPUT_FOLDER
python caj_to_pdf.py --recursive -o C:\pdfs C:\caj
python caj_to_pdf.py -o C:\pdfs a.caj b.caj
```

On Windows, `run.bat` accepts the same arguments and prefers the `py` launcher when it is installed.

If you omit `-o`, PDFs are written to a `pdf_out` folder next to the first input. Each PDF keeps the source file name. A name that is already taken gets `_2`, `_3`, and so on.

Check the converter without converting real files:

```
python caj_to_pdf.py --self-test
```

The process exits with code `0` when every file converted, and `1` when a path was missing, nothing was found, or any file was skipped or failed.

## What it converts

A `.caj` file is not one format. The first bytes decide which kind it is.

| Kind | What the script does |
| --- | --- |
| CAJ | The pages are already PDF objects, with the catalog and xref table left out. Those pieces are put back and written as a PDF. Page size stays as it was in the journal file. |
| `%PDF` | The file is already a PDF that was given a `.caj` name. It is copied. |
| KDH | The PDF bytes are masked. The mask is removed, then the PDF is saved. |
| HN / C8 | Each page is a JPEG image. Those images are placed on PDF pages. |
| TEB | Not converted. Open the file in CAJViewer and export or print it to PDF. |

HN files whose pages are JBIG or JBIG2 scans, and HN files that store the article as text rather than as images, are reported and skipped. Plain Python has no JBIG decoder. Open those files in CAJViewer and export or print them to PDF. If a file mixes JPEG pages with JBIG pages, the JPEG pages are written and the JBIG pages are left out.

The log line for each file is `OK`, `SKIP`, or `FAIL`, followed by a short reason.
