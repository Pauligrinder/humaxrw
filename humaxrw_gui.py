#!/usr/bin/env python3
"""Minimal tkinter GUI for HumaxRW — open image/drive, list, extract."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from drives import DiskDevice, elevation_hint, list_physical_disks
from humaxrw import HumaxDisk, Recording, extract_recording, human_size, VERSION


class HumaxRWApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"HumaxRW {VERSION}")
        self.minsize(720, 420)
        self.geometry("900x520")

        self._disk: Optional[HumaxDisk] = None
        self._source: str = ""
        self._recordings: list[Recording] = []
        self._extract_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._ui_queue: queue.Queue = queue.Queue()

        self._build()
        self._show_open()
        self.after(100, self._drain_queue)

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.open_frame = ttk.Frame(self, padding=16)
        self.open_frame.columnconfigure(0, weight=1)

        ttk.Label(
            self.open_frame,
            text="HumaxRW",
            font=("TkDefaultFont", 18, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(
            self.open_frame,
            text="Open a Humax 9200-series disk image or raw drive, then extract recordings.",
            wraplength=640,
        ).grid(row=1, column=0, sticky="w", pady=(0, 16))

        btn_row = ttk.Frame(self.open_frame)
        btn_row.grid(row=2, column=0, sticky="w")
        ttk.Button(btn_row, text="Open disk image…", command=self._open_image).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(btn_row, text="Open drive…", command=self._open_drive_dialog).pack(
            side="left"
        )

        self.hint = ttk.Label(
            self.open_frame,
            text=elevation_hint(),
            foreground="#555",
            wraplength=640,
        )
        self.hint.grid(row=3, column=0, sticky="w", pady=(16, 0))

        # Recordings view
        self.rec_frame = ttk.Frame(self, padding=8)
        self.rec_frame.columnconfigure(0, weight=1)
        self.rec_frame.rowconfigure(1, weight=1)

        top = ttk.Frame(self.rec_frame)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="← Back", command=self._close_disk).grid(row=0, column=0)
        self.source_label = ttk.Label(top, text="")
        self.source_label.grid(row=0, column=1, sticky="w", padx=12)
        ttk.Button(top, text="Extract…", command=self._extract).grid(row=0, column=2)

        cols = ("no", "size", "start", "channel", "title")
        self.tree = ttk.Treeview(
            self.rec_frame,
            columns=cols,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("no", text="No")
        self.tree.heading("size", text="Size")
        self.tree.heading("start", text="Start")
        self.tree.heading("channel", text="Channel")
        self.tree.heading("title", text="Title")
        self.tree.column("no", width=60, anchor="e", stretch=False)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("start", width=150, stretch=False)
        self.tree.column("channel", width=100, stretch=False)
        self.tree.column("title", width=400)
        self.tree.grid(row=1, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(self.rec_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        self.status = ttk.Label(self.rec_frame, text="")
        self.status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.progress = ttk.Progressbar(self.rec_frame, mode="determinate", maximum=100)
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.cancel_btn = ttk.Button(
            self.rec_frame, text="Cancel extract", command=self._cancel_extract, state="disabled"
        )
        self.cancel_btn.grid(row=4, column=0, columnspan=2, sticky="e", pady=(4, 0))

    def _show_open(self) -> None:
        self.rec_frame.grid_remove()
        self.open_frame.grid(row=0, column=0, sticky="nsew")

    def _show_recordings(self) -> None:
        self.open_frame.grid_remove()
        self.rec_frame.grid(row=0, column=0, sticky="nsew")

    # --- open ---------------------------------------------------------------

    def _open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Humax disk image",
            filetypes=[
                ("Disk images", "*.img *.dd *.raw *.bin"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._load_source(path)

    def _open_drive_dialog(self) -> None:
        disks = list_physical_disks()
        win = tk.Toplevel(self)
        win.title("Open drive")
        win.transient(self)
        win.grab_set()
        win.minsize(480, 280)
        win.geometry("560x320")

        ttk.Label(
            win,
            text="Select a whole disk (raw device). Opening usually needs elevated rights.",
            wraplength=520,
        ).pack(anchor="w", padx=12, pady=(12, 8))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12, pady=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        lb = tk.Listbox(frame, activestyle="dotbox")
        lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        lb.configure(yscrollcommand=sb.set)

        items: list[DiskDevice] = disks
        if not items:
            lb.insert("end", "(no disks found)")
            lb.configure(state="disabled")
        else:
            for d in items:
                lb.insert("end", d.display)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=12)

        def do_open() -> None:
            if not items:
                return
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Open drive", "Select a drive first.", parent=win)
                return
            device = items[sel[0]]
            win.destroy()
            self._load_source(device.path)

        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Open", command=do_open).pack(side="right", padx=(0, 8))
        if items:
            lb.selection_set(0)
            lb.bind("<Double-Button-1>", lambda _e: do_open())

    def _load_source(self, path: str) -> None:
        self._close_disk(reset_ui=False)
        try:
            disk = HumaxDisk(path)
            disk.open()
            recs = disk.recordings()
        except PermissionError as exc:
            messagebox.showerror(
                "Permission denied",
                f"Cannot open {path}:\n{exc}\n\n{elevation_hint()}",
                parent=self,
            )
            return
        except OSError as exc:
            messagebox.showerror("Open failed", f"Cannot open {path}:\n{exc}", parent=self)
            return
        except Exception as exc:  # noqa: BLE001 — show any parse/format error
            messagebox.showerror(
                "Not a Humax disk?",
                f"Failed to read Humax layout from {path}:\n{exc}",
                parent=self,
            )
            return

        self._disk = disk
        self._source = path
        self._recordings = recs
        self._populate_tree()
        self.source_label.configure(text=path)
        self.status.configure(text=f"{len(recs)} recording(s)")
        self.progress["value"] = 0
        self._show_recordings()

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for rec in self._recordings:
            start = ""
            if rec.start_dt is not None:
                start = rec.start_dt.strftime("%Y-%m-%d %H:%M")
            self.tree.insert(
                "",
                "end",
                iid=str(rec.number),
                values=(
                    rec.number,
                    human_size(rec.size),
                    start,
                    rec.channel or "",
                    rec.title or f"{rec.number}.av",
                ),
            )

    def _close_disk(self, reset_ui: bool = True) -> None:
        if self._extract_thread and self._extract_thread.is_alive():
            messagebox.showwarning(
                "Busy",
                "An extract is still running. Cancel it first.",
                parent=self,
            )
            return
        if self._disk is not None:
            try:
                self._disk.close()
            except Exception:  # noqa: BLE001
                pass
        self._disk = None
        self._source = ""
        self._recordings = []
        self.tree.delete(*self.tree.get_children())
        if reset_ui:
            self._show_open()

    # --- extract ------------------------------------------------------------

    def _selected_recordings(self) -> list[Recording]:
        selected = set(self.tree.selection())
        return [r for r in self._recordings if str(r.number) in selected]

    def _extract(self) -> None:
        if self._disk is None:
            return
        if self._extract_thread and self._extract_thread.is_alive():
            messagebox.showinfo("Busy", "An extract is already running.", parent=self)
            return
        recs = self._selected_recordings()
        if not recs:
            messagebox.showinfo("Extract", "Select one or more recordings first.", parent=self)
            return
        out = filedialog.askdirectory(title="Choose output folder")
        if not out:
            return
        out_dir = Path(out)
        self._cancel.clear()
        self.cancel_btn.configure(state="normal")
        self.progress["value"] = 0
        self.status.configure(text=f"Extracting 0 / {len(recs)}…")

        def worker() -> None:
            done = 0
            errors: list[str] = []
            for rec in recs:
                if self._cancel.is_set():
                    self._ui_queue.put(("status", "Extract cancelled."))
                    break

                def on_progress(written: int, total: int, name: str) -> None:
                    if self._cancel.is_set():
                        raise InterruptedError("cancelled")
                    pct = min(100.0, 100.0 * written / max(total, 1))
                    overall = (done + written / max(total, 1)) / len(recs) * 100.0
                    self._ui_queue.put(
                        (
                            "progress",
                            overall,
                            f"[{done + 1}/{len(recs)}] {name}: "
                            f"{human_size(written)} / {human_size(total)} ({pct:.0f}%)",
                        )
                    )

                try:
                    extract_recording(
                        self._disk,  # type: ignore[arg-type]
                        rec,
                        out_dir,
                        progress=False,
                        on_progress=on_progress,
                    )
                except InterruptedError:
                    self._ui_queue.put(("status", "Extract cancelled."))
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{rec.number}: {exc}")
                done += 1
                self._ui_queue.put(
                    ("progress", done / len(recs) * 100.0, f"Finished {done} / {len(recs)}")
                )

            if not self._cancel.is_set() and not errors:
                self._ui_queue.put(("done", f"Extracted {done} recording(s) to {out_dir}"))
            elif errors:
                self._ui_queue.put(
                    ("error", f"Finished with errors:\n" + "\n".join(errors[:8]))
                )
            else:
                self._ui_queue.put(("done", "Extract cancelled."))
            self._ui_queue.put(("idle", None))

        self._extract_thread = threading.Thread(target=worker, daemon=True)
        self._extract_thread.start()

    def _cancel_extract(self) -> None:
        self._cancel.set()
        self.status.configure(text="Cancelling…")

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, overall, text = msg
                    self.progress["value"] = overall
                    self.status.configure(text=text)
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "done":
                    self.progress["value"] = 100
                    self.status.configure(text=msg[1])
                    messagebox.showinfo("Extract", msg[1], parent=self)
                elif kind == "error":
                    self.status.configure(text="Extract finished with errors.")
                    messagebox.showerror("Extract", msg[1], parent=self)
                elif kind == "idle":
                    self.cancel_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def destroy(self) -> None:
        self._cancel.set()
        if self._disk is not None:
            try:
                self._disk.close()
            except Exception:  # noqa: BLE001
                pass
        super().destroy()


def main() -> None:
    app = HumaxRWApp()
    app.mainloop()


if __name__ == "__main__":
    main()
