#!/usr/bin/env python3
"""Tkinter UI wrapper for pipeline_config_tool.py."""

import csv
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlparse

import yaml


CONFIG_BASE_PATH = r"C:\git\64bit\argus-config"
CONFIGS_PATH = CONFIG_BASE_PATH + r"\configs"
DEPLOYMENT_MAP_PATH = CONFIG_BASE_PATH + r"\deployment_map.csv"


def _load_gps_from_config(value: str) -> str:
    stripped = (value or "").strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    return stripped


class PipelineConfigToolUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Pipeline Config Tool UI")
        self.root.geometry("1200x820")
        self.root.minsize(960, 640)

        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.device_id_var = tk.StringVar()
        self.pipeline_number_var = tk.StringVar()
        self.input_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")
        self.pipeline_summary_var = tk.StringVar(value="Loading pipeline list...")
        self.search_var = tk.StringVar()
        self.grouped_pipeline_data: list[dict[str, object]] = []
        self._current_output_line = ""
        self._suppressed_prompt_line = ""
        self.prompt_dialog: tk.Toplevel | None = None
        self.prompt_entry_var = tk.StringVar()
        self.prompt_title_var = tk.StringVar(value="Input requested")
        self.prompt_desc_var = tk.StringVar(value="")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_output_queue)
        # Load pipeline data after the window appears to keep startup responsive.
        self.root.after(50, self._load_existing_pipelines)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        browser = ttk.LabelFrame(container, text="Existing Pipelines", padding=12)
        browser.pack(fill="both", expand=True)

        browser_toolbar = ttk.Frame(browser)
        browser_toolbar.pack(fill="x")
        ttk.Label(browser_toolbar, textvariable=self.pipeline_summary_var).pack(side="left")
        ttk.Button(browser_toolbar, text="Clear", command=self._clear_search).pack(side="right", padx=(8, 0))
        search_entry = ttk.Entry(browser_toolbar, textvariable=self.search_var, width=34)
        search_entry.pack(side="right")
        search_entry.bind("<KeyRelease>", self._apply_search_filter_event)
        ttk.Label(browser_toolbar, text="Search").pack(side="right", padx=(12, 6))
        self.refresh_button = ttk.Button(browser_toolbar, text="Refresh", command=self._load_existing_pipelines)
        self.refresh_button.pack(side="right")
        self.loading_bar = ttk.Progressbar(browser_toolbar, mode="indeterminate", length=140)

        tree_frame = ttk.Frame(browser)
        tree_frame.pack(fill="both", expand=True, pady=(8, 0))

        columns = (
            "type",
            "user",
            "host_name",
            "intersection_address",
            "gps_coordinates",
            "front_camera_ip",
            "back_camera_ip",
            "direction",
        )
        self.pipeline_tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", height=12)
        self.pipeline_tree.heading("#0", text="Device / Pipeline")
        self.pipeline_tree.heading("type", text="Type")
        self.pipeline_tree.heading("user", text="User")
        self.pipeline_tree.heading("host_name", text="Host")
        self.pipeline_tree.heading("intersection_address", text="Intersection")
        self.pipeline_tree.heading("gps_coordinates", text="GPS")
        self.pipeline_tree.heading("front_camera_ip", text="Front Camera")
        self.pipeline_tree.heading("back_camera_ip", text="Back Camera")
        self.pipeline_tree.heading("direction", text="Direction")

        self.pipeline_tree.column("#0", width=160, stretch=False)
        self.pipeline_tree.column("type", width=80, stretch=False)
        self.pipeline_tree.column("user", width=120, stretch=False)
        self.pipeline_tree.column("host_name", width=120, stretch=False)
        self.pipeline_tree.column("intersection_address", width=220)
        self.pipeline_tree.column("gps_coordinates", width=150, stretch=False)
        self.pipeline_tree.column("front_camera_ip", width=120, stretch=False)
        self.pipeline_tree.column("back_camera_ip", width=120, stretch=False)
        self.pipeline_tree.column("direction", width=180)
        self.pipeline_tree.pack(side="left", fill="both", expand=True)
        self.pipeline_tree.bind("<<TreeviewSelect>>", self._handle_tree_selection)

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.pipeline_tree.yview)
        tree_scroll_y.pack(side="right", fill="y")
        self.pipeline_tree.configure(yscrollcommand=tree_scroll_y.set)

        tree_scroll_x = ttk.Scrollbar(browser, orient="horizontal", command=self.pipeline_tree.xview)
        tree_scroll_x.pack(fill="x")
        self.pipeline_tree.configure(xscrollcommand=tree_scroll_x.set)

        form = ttk.LabelFrame(container, text="Run Pipeline Config Tool", padding=12)
        form.pack(fill="x", pady=(12, 0))

        ttk.Label(form, text="Device ID").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.device_id_var, width=24).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Pipeline Number").grid(row=0, column=2, sticky="w", padx=(16, 8), pady=4)
        ttk.Entry(form, textvariable=self.pipeline_number_var, width=16).grid(row=0, column=3, sticky="ew", pady=4)

        button_bar = ttk.Frame(form)
        button_bar.grid(row=1, column=0, columnspan=4, sticky="e", pady=4)

        self.run_button = ttk.Button(button_bar, text="Run", command=self._start_process)
        self.run_button.pack(side="left", padx=(0, 8))

        self.stop_button = ttk.Button(button_bar, text="Stop", command=self._stop_process, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))

        ttk.Button(button_bar, text="Clear Output", command=self._clear_output).pack(side="left")

        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        self.status_frame = ttk.Frame(container)
        self.status_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(self.status_frame, text="Status:").pack(side="left")
        ttk.Label(self.status_frame, textvariable=self.status_var).pack(side="left", padx=(6, 0))

        output_frame = ttk.LabelFrame(container, text="Output", padding=12)
        output_frame.pack(fill="both", expand=True, pady=(12, 0))

        self.output_text = ScrolledText(output_frame, wrap="word", font=("Consolas", 10))
        self.output_text.pack(fill="both", expand=True)
        self.output_text.configure(state="disabled")

    def _looks_like_input_prompt(self) -> bool:
        # input() prompts in pipeline_config_tool.py are printed without newline and end with ': '.
        return bool(re.search(r"[A-Za-z0-9_\-() ]+:\s*$", self._current_output_line))

    def _prompt_description(self, prompt_text: str) -> str:
        prompt_lower = prompt_text.lower()
        if "front_camera_ip" in prompt_lower:
            return "Enter front camera IP address (for example: 192.168.100.101)."
        if "back_camera_ip" in prompt_lower:
            return "Enter back camera IP address (for example: 192.168.100.102)."
        if "gps_coordinates" in prompt_lower:
            return "Enter GPS coordinates as two numeric values separated by comma (for example: 41.340081, 69.250844)."
        if "crossroad_name" in prompt_lower or "intersection_address" in prompt_lower:
            return "Enter intersection/crossroad name."
        if "direction" in prompt_lower:
            return "Enter direction text for this pipeline."
        if "user" in prompt_lower:
            return "Enter SSH username."
        if "host_name" in prompt_lower:
            return "Enter SSH host or IP address."
        return "The script is requesting additional input."

    def _center_dialog_over_root(self, dialog: tk.Toplevel, width: int = 520, height: int = 180) -> None:
        self.root.update_idletasks()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()

        x = root_x + max((root_w - width) // 2, 0)
        y = root_y + max((root_h - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    def _show_prompt_dialog(self, prompt_text: str) -> None:
        if self.prompt_dialog and self.prompt_dialog.winfo_exists():
            self.prompt_title_var.set(prompt_text)
            self.prompt_desc_var.set(self._prompt_description(prompt_text))
            self.prompt_dialog.lift()
            self.prompt_dialog.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Interactive Input Required")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        self._center_dialog_over_root(dialog)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)

        self.prompt_title_var.set(prompt_text)
        self.prompt_desc_var.set(self._prompt_description(prompt_text))

        ttk.Label(frame, textvariable=self.prompt_title_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.prompt_desc_var, wraplength=460, justify="left").pack(anchor="w", pady=(6, 8))

        prompt_entry = ttk.Entry(frame, textvariable=self.prompt_entry_var, width=60)
        prompt_entry.pack(fill="x")
        prompt_entry.bind("<Return>", self._submit_prompt_dialog_input_event)

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="Submit", command=self._submit_prompt_dialog_input).pack(side="right")
        ttk.Button(button_row, text="Cancel", command=self._cancel_prompt_dialog).pack(side="right", padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", self._cancel_prompt_dialog)

        self.prompt_dialog = dialog
        prompt_entry.focus_set()

    def _close_prompt_dialog(self) -> None:
        if self.prompt_dialog and self.prompt_dialog.winfo_exists():
            self.prompt_dialog.grab_release()
            self.prompt_dialog.destroy()
        self.prompt_dialog = None
        self.prompt_entry_var.set("")

    def _cancel_prompt_dialog(self) -> None:
        self._suppressed_prompt_line = self._current_output_line
        self._close_prompt_dialog()

    def _submit_prompt_dialog_input_event(self, _event: tk.Event) -> None:
        self._submit_prompt_dialog_input()

    def _submit_prompt_dialog_input(self) -> None:
        self._send_input(self.prompt_entry_var.get())

    def _read_yaml_file(self, path: str) -> dict:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as file_handle:
            data = yaml.safe_load(file_handle) or {}
        if isinstance(data, dict):
            return data
        return {}

    def _read_deployment_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        with open(DEPLOYMENT_MAP_PATH, newline="", encoding="utf-8") as file_handle:
            first_line = file_handle.readline()
            if not first_line.lower().startswith("sep="):
                file_handle.seek(0)
            reader = csv.DictReader(file_handle)
            for row in reader:
                rows.append(row)
        return rows

    def _build_grouped_pipeline_data(self) -> list[dict[str, object]]:
        grouped: dict[str, dict[str, object]] = {}
        rows = self._read_deployment_rows()

        for row in rows:
            device_id = (row.get("device_id") or "").strip()
            pipeline_number = (row.get("pipeline_number") or "").strip()
            user = (row.get("user") or "").strip()
            host_name = (row.get("host_name") or "").strip()
            pipeline_config_file_name = (row.get("pipeline_config_file_name") or "").strip()
            pipeline_config_path = os.path.join(CONFIGS_PATH, pipeline_config_file_name)
            pipeline_cfg = self._read_yaml_file(pipeline_config_path)

            io_block = pipeline_cfg.get("io", {}) if isinstance(pipeline_cfg.get("io", {}), dict) else {}
            fixation_block = pipeline_cfg.get("fixation", {}) if isinstance(pipeline_cfg.get("fixation", {}), dict) else {}

            front_url = io_block.get("input_video_path", "")
            back_url = io_block.get("input_traffic_light_video_path", "")
            front_camera_ip = urlparse(front_url).hostname or ""
            back_camera_ip = urlparse(back_url).hostname or ""
            direction = (fixation_block.get("direction", "") or "").strip()
            intersection_address = (fixation_block.get("intersection_address", "") or "").strip()
            gps_coordinates = _load_gps_from_config(fixation_block.get("gps_coordinates", ""))

            if device_id not in grouped:
                grouped[device_id] = {
                    "device_id": device_id,
                    "user": user,
                    "host_name": host_name,
                    "intersection_address": intersection_address,
                    "gps_coordinates": gps_coordinates,
                    "pipelines": [],
                }

            group = grouped[device_id]
            if not group["user"] and user:
                group["user"] = user
            if not group["host_name"] and host_name:
                group["host_name"] = host_name
            if not group["intersection_address"] and intersection_address:
                group["intersection_address"] = intersection_address
            if not group["gps_coordinates"] and gps_coordinates:
                group["gps_coordinates"] = gps_coordinates

            pipelines = group["pipelines"]
            if isinstance(pipelines, list):
                pipelines.append(
                    {
                        "pipeline_number": pipeline_number,
                        "front_camera_ip": front_camera_ip,
                        "back_camera_ip": back_camera_ip,
                        "direction": direction,
                    }
                )

        grouped_list = list(grouped.values())
        grouped_list.sort(key=lambda item: str(item["device_id"]))
        for item in grouped_list:
            pipelines = item["pipelines"]
            if isinstance(pipelines, list):
                pipelines.sort(key=lambda pipeline: int(pipeline["pipeline_number"]) if str(pipeline["pipeline_number"]).isdigit() else str(pipeline["pipeline_number"]))
        return grouped_list

    def _load_existing_pipelines(self) -> None:
        self._set_pipeline_loading(True)
        threading.Thread(target=self._load_existing_pipelines_worker, daemon=True).start()

    def _set_pipeline_loading(self, is_loading: bool) -> None:
        if is_loading:
            self.pipeline_summary_var.set("Loading pipelines...")
            self.refresh_button.configure(state="disabled")
            self.loading_bar.pack(side="right", padx=(8, 8))
            self.loading_bar.start(10)
        else:
            self.refresh_button.configure(state="normal")
            self.loading_bar.stop()
            self.loading_bar.pack_forget()

    def _load_existing_pipelines_worker(self) -> None:
        try:
            grouped_data = self._build_grouped_pipeline_data()
            self.output_queue.put(("pipelines_loaded", grouped_data))
        except FileNotFoundError:
            self.output_queue.put(("pipelines_error", f"Deployment map not found: {DEPLOYMENT_MAP_PATH}"))
        except Exception as exc:
            self.output_queue.put(("pipelines_error", f"Failed to load pipelines: {exc}"))

    def _group_matches_query(self, group: dict[str, object], query_lower: str) -> bool:
        if not query_lower:
            return True
        group_text = " ".join(
            [
                str(group.get("device_id", "")),
                str(group.get("user", "")),
                str(group.get("host_name", "")),
                str(group.get("intersection_address", "")),
                str(group.get("gps_coordinates", "")),
            ]
        ).lower()
        return query_lower in group_text

    def _pipeline_matches_query(self, pipeline: dict[str, str], query_lower: str) -> bool:
        if not query_lower:
            return True
        pipeline_text = " ".join(
            [
                str(pipeline.get("pipeline_number", "")),
                str(pipeline.get("front_camera_ip", "")),
                str(pipeline.get("back_camera_ip", "")),
                str(pipeline.get("direction", "")),
            ]
        ).lower()
        return query_lower in pipeline_text

    def _apply_search_filter_event(self, _event: tk.Event) -> None:
        self._apply_search_filter()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self._apply_search_filter()

    def _apply_search_filter(self) -> None:
        self.pipeline_tree.delete(*self.pipeline_tree.get_children())
        query = self.search_var.get().strip()
        query_lower = query.lower()

        visible_group_count = 0
        visible_pipeline_count = 0

        for group in self.grouped_pipeline_data:
            pipelines = group.get("pipelines", [])
            if not isinstance(pipelines, list):
                continue

            group_matches = self._group_matches_query(group, query_lower)
            if group_matches:
                visible_pipelines = pipelines
            else:
                visible_pipelines = [
                    pipeline
                    for pipeline in pipelines
                    if isinstance(pipeline, dict) and self._pipeline_matches_query(pipeline, query_lower)
                ]

            if not group_matches and not visible_pipelines:
                continue

            visible_group_count += 1
            parent_id = self.pipeline_tree.insert(
                "",
                "end",
                text=str(group["device_id"]),
                values=(
                    "device",
                    group["user"],
                    group["host_name"],
                    group["intersection_address"],
                    group["gps_coordinates"],
                    "",
                    "",
                    "",
                ),
                tags=("device",),
            )

            for pipeline in visible_pipelines:
                visible_pipeline_count += 1
                self.pipeline_tree.insert(
                    parent_id,
                    "end",
                    text=f"pipeline {pipeline['pipeline_number']}",
                    values=(
                        "pipeline",
                        "",
                        "",
                        "",
                        "",
                        pipeline["front_camera_ip"],
                        pipeline["back_camera_ip"],
                        pipeline["direction"],
                    ),
                    tags=("pipeline", str(group["device_id"]), str(pipeline["pipeline_number"])),
                )
            self.pipeline_tree.item(parent_id, open=True)

        if query:
            self.pipeline_summary_var.set(
                f"Showing {visible_pipeline_count} pipelines across {visible_group_count} device groups (filtered)"
            )
        else:
            self.pipeline_summary_var.set(
                f"Loaded {visible_pipeline_count} pipelines across {visible_group_count} device groups"
            )

    def _handle_tree_selection(self, _event: tk.Event) -> None:
        selected = self.pipeline_tree.selection()
        if not selected:
            return

        item_id = selected[0]
        parent_id = self.pipeline_tree.parent(item_id)
        if not parent_id:
            self.device_id_var.set(self.pipeline_tree.item(item_id, "text"))
            return

        device_id = self.pipeline_tree.item(parent_id, "text")
        pipeline_text = self.pipeline_tree.item(item_id, "text")
        pipeline_number = pipeline_text.replace("pipeline", "", 1).strip()
        self.device_id_var.set(device_id)
        self.pipeline_number_var.set(pipeline_number)

    def _script_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_config_tool.py")

    def _append_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text)
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _clear_output(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")

    def _validate_inputs(self) -> tuple[str, str] | None:
        device_id = self.device_id_var.get().strip()
        pipeline_number = self.pipeline_number_var.get().strip()

        if not device_id:
            messagebox.showerror("Missing Device ID", "Device ID is required.")
            return None

        if not pipeline_number:
            messagebox.showerror("Missing Pipeline Number", "Pipeline number is required.")
            return None

        try:
            int(pipeline_number)
        except ValueError:
            messagebox.showerror("Invalid Pipeline Number", "Pipeline number must be an integer.")
            return None

        return device_id, pipeline_number

    def _set_running_state(self, is_running: bool) -> None:
        self.run_button.configure(state="disabled" if is_running else "normal")
        self.stop_button.configure(state="normal" if is_running else "disabled")
        self.status_var.set("Running" if is_running else "Idle")
        if not is_running:
            self._close_prompt_dialog()
            self._current_output_line = ""
            self._suppressed_prompt_line = ""

    def _start_process(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("Already Running", "The tool is already running.")
            return

        validated = self._validate_inputs()
        if validated is None:
            return

        device_id, pipeline_number = validated
        password = simpledialog.askstring(
            "SSH Password",
            "Enter SSH password:",
            parent=self.root,
            show="*",
        )
        if password is None:
            return
        if not password:
            messagebox.showerror("Missing Password", "Password is required.")
            return

        command = [
            sys.executable,
            "-u",
            self._script_path(),
            device_id,
            pipeline_number,
            "--password",
            password,
        ]

        self._append_output(f"$ {' '.join(command[:-1])} ********\n")

        try:
            self.process = subprocess.Popen(
                command,
                cwd=os.path.dirname(self._script_path()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Failed to Start", str(exc))
            self.process = None
            return

        self._set_running_state(True)
        self.input_var.set("")
        threading.Thread(target=self._read_process_output, daemon=True).start()
        threading.Thread(target=self._wait_for_exit, daemon=True).start()

    def _read_process_output(self) -> None:
        if not self.process or not self.process.stdout:
            return

        while True:
            char = self.process.stdout.read(1)
            if char == "":
                break
            self.output_queue.put(("output", char))

    def _wait_for_exit(self) -> None:
        if not self.process:
            return
        return_code = self.process.wait()
        self.output_queue.put(("exit", str(return_code)))

    def _poll_output_queue(self) -> None:
        while True:
            try:
                event_type, payload = self.output_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "output":
                text_chunk = str(payload)
                self._append_output(text_chunk)
                for ch in text_chunk:
                    if ch in "\r\n":
                        self._current_output_line = ""
                        self._suppressed_prompt_line = ""
                        self._close_prompt_dialog()
                    else:
                        self._current_output_line += ch
                if self._looks_like_input_prompt():
                    prompt_text = self._current_output_line.strip()
                    if prompt_text and prompt_text != self._suppressed_prompt_line:
                        self._show_prompt_dialog(prompt_text)
            elif event_type == "exit":
                self._append_output(f"\n[process exited with code {payload}]\n")
                self.process = None
                self._set_running_state(False)
            elif event_type == "pipelines_loaded":
                self.grouped_pipeline_data = payload if isinstance(payload, list) else []
                self._apply_search_filter()
                self._set_pipeline_loading(False)
            elif event_type == "pipelines_error":
                self.pipeline_tree.delete(*self.pipeline_tree.get_children())
                self.pipeline_summary_var.set(str(payload))
                self._set_pipeline_loading(False)

        self.root.after(100, self._poll_output_queue)

    def _send_input(self, value: str) -> None:
        value = value if value is not None else ""
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            return

        try:
            self.process.stdin.write(value + "\n")
            self.process.stdin.flush()
            self._append_output(f"\n> {value}\n")
            self.prompt_entry_var.set("")
            self._close_prompt_dialog()
            self._current_output_line = ""
            self._suppressed_prompt_line = ""
        except Exception as exc:
            messagebox.showerror("Failed to Send Input", str(exc))

    def _stop_process(self) -> None:
        if not self.process or self.process.poll() is not None:
            return

        try:
            self.process.terminate()
            self._append_output("\n[stop requested]\n")
        except Exception as exc:
            messagebox.showerror("Failed to Stop", str(exc))

    def _on_close(self) -> None:
        self._close_prompt_dialog()
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Exit", "A process is still running. Stop it and close?"):
                return
            self._stop_process()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = PipelineConfigToolUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()