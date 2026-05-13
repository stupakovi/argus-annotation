#!/usr/bin/env python3
"""
Lookup a deployment map record by device_id and pipeline_number,
and manage the SSH port-forwarding tunnel for its cameras.

Usage:
    python pipeline_config_tool.py <device_id> <pipeline_number> --password <ssh_password>
    
Requirements:
    - paramiko: Install with `pip install paramiko`
    - sshtunnel: Install with `pip install sshtunnel`
"""

import argparse
import atexit
import csv
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import urlparse

import cv2
import yaml
from sshtunnel import SSHTunnelForwarder

# Global dictionary to store active SSH tunnels
_active_tunnels = {}


def _cleanup_tunnels():
    """Cleanup all active tunnels on exit."""
    for key, tunnel in list(_active_tunnels.items()):
        try:
            if tunnel.is_active:
                tunnel.stop()
                print(f"Closed tunnel: {key}", file=sys.stderr)
        except Exception as e:
            print(f"Error closing tunnel {key}: {e}", file=sys.stderr)
    _active_tunnels.clear()


# Register cleanup handler
atexit.register(_cleanup_tunnels)

CONFIG_BASE_PATH = r"C:\git\64bit\argus-config"
CONFIGS_PATH = CONFIG_BASE_PATH + r"\configs"
DEPLOYMENT_MAP_PATH = CONFIG_BASE_PATH + r"\deployment_map.csv"
PID_DIR = CONFIG_BASE_PATH + r"\pids"
CAMERA_FRAME_BASE_PATH = r"C:\temp\64bit\devices"
CONFIGS_PATH = CONFIG_BASE_PATH + r"\configs"
DEPLOYMENT_MAP_PATH = CONFIG_BASE_PATH + r"\deployment_map.csv"
PID_DIR = CONFIG_BASE_PATH + r"\pids"
CAMERA_FRAME_BASE_PATH = r"C:\temp\64bit\devices"


def _pid_file(device_id: str, pipeline_number: int) -> str:
    return os.path.join(PID_DIR, f"tunnel-{device_id}-{pipeline_number}.pid")


def _prompt_required(prompt_text: str) -> str:
    """Prompt until a non-empty value is provided."""
    while True:
        value = input(prompt_text).strip()
        if value:
            return value
        print("Value cannot be empty. Please try again.")


def _is_ascii_string(value: str) -> bool:
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _prompt_ascii(prompt_text: str) -> str:
    """Prompt until a non-empty ASCII-only value is provided."""
    while True:
        value = _prompt_required(prompt_text)
        if _is_ascii_string(value):
            return value
        print("Value must contain ASCII characters only. Please try again.")


def _is_valid_gps_coordinates(value: str) -> bool:
    """Validate GPS as two numeric values separated by comma."""
    return bool(re.fullmatch(r"\s*[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d+(?:\.\d+)?\s*", value))


def _prompt_gps(prompt_text: str) -> str:
    """Prompt until valid GPS coordinates are provided."""
    while True:
        value = _prompt_required(prompt_text)
        if _is_valid_gps_coordinates(value):
            return value
        print("GPS must be two numeric values separated by comma (example: 41.340081, 69.250844).")


def _sanitize_ascii_or_prompt(value: str, prompt_text: str) -> str:
    """Use existing ASCII value if valid; otherwise ask interactively."""
    sanitized = (value or "").strip()
    if sanitized and _is_ascii_string(sanitized):
        return sanitized
    print(f"Invalid value for {prompt_text.strip(': ')}. Please enter a valid ASCII value.")
    return _prompt_ascii(prompt_text)


def _strip_gps_parentheses(value: str) -> str:
    """Remove surrounding parentheses and extra whitespace from GPS coordinates."""
    stripped = (value or "").strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    return stripped


def _load_gps_from_config(value: str) -> str:
    """Load GPS from config without validation; strip parentheses if present."""
    stripped = _strip_gps_parentheses(value)
    return stripped


def _sanitize_gps_or_prompt(value: str, prompt_text: str) -> str:
    """Use existing GPS value if valid; otherwise ask interactively."""
    sanitized = (value or "").strip()
    if _is_valid_gps_coordinates(sanitized):
        return sanitized
    print(
        f"Invalid value for {prompt_text.strip(': ')}. "
        "Please enter two numeric values separated by comma."
    )
    return _prompt_gps(prompt_text)


def _find_existing_pipeline(device_id: str, pipeline_number: int) -> tuple:
    """
    Search for any existing pipeline with the same device_id.
    Returns (user, host_name, intersection_address, gps_coordinates, pipeline_config_path)
    or (None, None, None, None, None) if not found.
    """
    try:
        with open(DEPLOYMENT_MAP_PATH, newline="", encoding="utf-8") as fh:
            # Skip leading sep= line if present
            first_line = fh.readline()
            if not first_line.lower().startswith("sep="):
                fh.seek(0)
            reader = csv.DictReader(fh)
            for row in reader:
                if row["device_id"] == device_id:
                    # Found a pipeline for this device_id
                    user = row["user"]
                    host_name = row["host_name"]
                    pipeline_config_file_name = row["pipeline_config_file_name"]
                    pipeline_config_path = os.path.join(CONFIGS_PATH, pipeline_config_file_name)
                    
                    # Load intersection_address and gps_coordinates from config
                    intersection_address = ""
                    gps_coordinates = ""
                    if os.path.isfile(pipeline_config_path):
                        with open(pipeline_config_path, "r", encoding="utf-8") as pf:
                            pipeline_cfg = yaml.safe_load(pf)
                        fixation_block = pipeline_cfg.get("fixation", {})
                        intersection_address = fixation_block.get("intersection_address", "")
                        gps_coordinates = _load_gps_from_config(fixation_block.get("gps_coordinates", ""))
                    
                    return user, host_name, intersection_address, gps_coordinates, pipeline_config_path
    except FileNotFoundError:
        pass
    
    return None, None, None, None, None


def _read_pid(path: str):
    if os.path.isfile(path):
        try:
            return int(open(path).read().strip())
        except ValueError:
            return None
    return None


def _is_running(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _start_tunnel(host: str, user: str, password: str, back_camera_ip: str, device_id: str, pipeline_number: int) -> None:
    """
    Start SSH tunnel for port forwarding using sshtunnel.
    Maps local port 18601 to remote back camera IP.
    """
    tunnel_key = f"{device_id}-{pipeline_number}"
    
    # Check if tunnel already exists and is active
    if tunnel_key in _active_tunnels:
        tunnel = _active_tunnels[tunnel_key]
        if tunnel.is_active:
            print(f"Tunnel already running for {tunnel_key}")
            return
        else:
            # Clean up dead tunnel
            del _active_tunnels[tunnel_key]
    
    os.makedirs(PID_DIR, exist_ok=True)
    
    try:
        # Create SSH tunnel with port forwarding
        tunnel = SSHTunnelForwarder(
            (host, 22),
            ssh_username=user,
            ssh_password=password,
            remote_bind_address=(back_camera_ip, 554),
            local_bind_address=("127.0.0.1", 18601),
            allow_agent=False,
            set_keepalive=5.0,
        )
        tunnel.start()
        _active_tunnels[tunnel_key] = tunnel
        
        # Save PID file for backwards compatibility
        pid_file_path = _pid_file(device_id, pipeline_number)
        with open(pid_file_path, "w") as f:
            f.write(str(os.getpid()))
        
        print(f"Tunnel started for {tunnel_key} on local port 18601")
        
    except Exception as e:
        print(f"Failed to start tunnel: {e}", file=sys.stderr)
        raise


def _start_second_tunnel(host: str, user: str, password: str, front_camera_ip: str, device_id: str, pipeline_number: int) -> None:
    """
    Start second SSH tunnel for front camera on port 18602.
    """
    tunnel_key_front = f"{device_id}-{pipeline_number}-front"
    
    if tunnel_key_front in _active_tunnels:
        tunnel = _active_tunnels[tunnel_key_front]
        if tunnel.is_active:
            print(f"Front camera tunnel already running for {tunnel_key_front}")
            return
        else:
            del _active_tunnels[tunnel_key_front]
    
    try:
        tunnel = SSHTunnelForwarder(
            (host, 22),
            ssh_username=user,
            ssh_password=password,
            remote_bind_address=(front_camera_ip, 554),
            local_bind_address=("127.0.0.1", 18602),
            allow_agent=False,
            set_keepalive=5.0,
        )
        tunnel.start()
        _active_tunnels[tunnel_key_front] = tunnel
        print(f"Tunnel started for {tunnel_key_front} on local port 18602")
        
    except Exception as e:
        print(f"Failed to start front camera tunnel: {e}", file=sys.stderr)
        raise


def _wait_for_tunnel(timeout: int = 30) -> bool:
    """
    Wait for tunnel to be ready by attempting to connect to forwarded ports.
    Returns True if tunnel is ready, False if timeout or connection fails.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Try to connect to localhost:18601 (back camera port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", 18601))
            sock.close()
            if result == 0:
                print("Tunnel is ready (port 18601 is accessible)")
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print("Timeout waiting for tunnel to be ready", file=sys.stderr)
    return False


def _stop_tunnel(device_id: str, pipeline_number: int) -> None:
    """Stop and close all tunnels for this device/pipeline."""
    tunnel_key = f"{device_id}-{pipeline_number}"
    tunnel_key_front = f"{device_id}-{pipeline_number}-front"
    
    for key in [tunnel_key, tunnel_key_front]:
        if key in _active_tunnels:
            try:
                _active_tunnels[key].stop()
                del _active_tunnels[key]
                print(f"Tunnel stopped: {key}")
            except Exception as e:
                print(f"Error closing tunnel {key}: {e}", file=sys.stderr)
    
    # Clean up PID file
    pf = _pid_file(device_id, pipeline_number)
    if os.path.isfile(pf):
        try:
            os.remove(pf)
        except Exception:
            pass


def _tunnel_status(device_id: str, pipeline_number: int) -> None:
    """Check tunnel status."""
    tunnel_key = f"{device_id}-{pipeline_number}"
    
    if tunnel_key in _active_tunnels:
        tunnel = _active_tunnels[tunnel_key]
        if tunnel.is_active:
            print(f"Tunnel: running ({tunnel_key})")
        else:
            print(f"Tunnel: not running ({tunnel_key})")
    else:
        print(f"Tunnel: not running (no active tunnel for {tunnel_key})")


def _rewrite_rtsp_url_for_tunnel(source_url: str, local_port: int, fallback_ip: str) -> str:
    """Reuse configured RTSP URL, replacing only host/port with the local SSH tunnel."""
    parsed = urlparse((source_url or "").strip())
    if parsed.scheme:
        credentials = ""
        if parsed.username:
            credentials = parsed.username
            if parsed.password is not None:
                credentials += f":{parsed.password}"
            credentials += "@"
        path = parsed.path or "/cam/realmonitor"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{credentials}127.0.0.1:{local_port}{path}{query}"

    return f"rtsp://admin:DOM2588205@127.0.0.1:{local_port}/cam/realmonitor?channel=1&subtype=0"


def _capture_frames_from_forwarded_ports(
    back_camera_ip: str,
    front_camera_ip: str,
    back_stream_url: str,
    front_stream_url: str,
) -> tuple:
    """
    Capture one frame from each forwarded port (18601=back, 18602=front).
    Returns (back_frame_path, front_frame_path) or (None, None) if capture fails.
    """
    print("Waiting for tunnel to stabilize...")
    time.sleep(2)

    temp_dir = tempfile.gettempdir()
    # Format: cam_[IP]_20260216_151721.jpg (using current timestamp)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    back_frame_path = os.path.join(temp_dir, f"cam_{back_camera_ip}_{timestamp}.jpg")
    front_frame_path = os.path.join(temp_dir, f"cam_{front_camera_ip}_{timestamp}.jpg")

    # Capture from back camera (port 18601)
    back_url = _rewrite_rtsp_url_for_tunnel(back_stream_url, 18601, back_camera_ip)
    print(f"Capturing frame from back camera: {back_url}")
    cap_back = cv2.VideoCapture(back_url)
    if cap_back.isOpened():
        ret, frame = cap_back.read()
        if ret:
            cv2.imwrite(back_frame_path, frame)
            print(f"Back camera frame saved: {back_frame_path}")
        else:
            print("Failed to read frame from back camera", file=sys.stderr)
            back_frame_path = None
        cap_back.release()
    else:
        print("Failed to open back camera stream", file=sys.stderr)
        back_frame_path = None

    # Capture from front camera (port 18602)
    front_url = _rewrite_rtsp_url_for_tunnel(front_stream_url, 18602, front_camera_ip)
    print(f"Capturing frame from front camera: {front_url}")
    cap_front = cv2.VideoCapture(front_url)
    if cap_front.isOpened():
        ret, frame = cap_front.read()
        if ret:
            cv2.imwrite(front_frame_path, frame)
            print(f"Front camera frame saved: {front_frame_path}")
        else:
            print("Failed to read frame from front camera", file=sys.stderr)
            front_frame_path = None
        cap_front.release()
    else:
        print("Failed to open front camera stream", file=sys.stderr)
        front_frame_path = None

    return back_frame_path, front_frame_path


def _capture_frames_to_folder(
    back_camera_ip: str,
    front_camera_ip: str,
    back_stream_url: str,
    front_stream_url: str,
    camera_frame_folder: str,
    user: str,
    host_name: str,
) -> tuple:
    """
    Capture one frame from each forwarded port into
    <camera_frame_folder>/<user>@<host_name>/cam_<ip>_<timestamp>.jpg
    and return (back_path, front_path, timestamp).
    """
    print("Waiting for tunnel to stabilize...")
    time.sleep(2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    device_folder = os.path.join(camera_frame_folder, f"{user}@{host_name}")
    os.makedirs(device_folder, exist_ok=True)

    back_ip_token = back_camera_ip.replace(".", "_")
    front_ip_token = front_camera_ip.replace(".", "_")
    back_frame_path = os.path.join(device_folder, f"cam_{back_ip_token}_{timestamp}.jpg")
    front_frame_path = os.path.join(device_folder, f"cam_{front_ip_token}_{timestamp}.jpg")

    back_url = _rewrite_rtsp_url_for_tunnel(back_stream_url, 18601, back_camera_ip)
    print(f"Capturing frame from back camera: {back_url}")
    cap_back = cv2.VideoCapture(back_url)
    if cap_back.isOpened():
        ret, frame = cap_back.read()
        if ret:
            cv2.imwrite(back_frame_path, frame)
            print(f"Back camera frame saved: {back_frame_path}")
        else:
            print("Failed to read frame from back camera", file=sys.stderr)
            back_frame_path = None
        cap_back.release()
    else:
        print("Failed to open back camera stream", file=sys.stderr)
        back_frame_path = None

    front_url = _rewrite_rtsp_url_for_tunnel(front_stream_url, 18602, front_camera_ip)
    print(f"Capturing frame from front camera: {front_url}")
    cap_front = cv2.VideoCapture(front_url)
    if cap_front.isOpened():
        ret, frame = cap_front.read()
        if ret:
            cv2.imwrite(front_frame_path, frame)
            print(f"Front camera frame saved: {front_frame_path}")
        else:
            print("Failed to read frame from front camera", file=sys.stderr)
            front_frame_path = None
        cap_front.release()
    else:
        print("Failed to open front camera stream", file=sys.stderr)
        front_frame_path = None

    return back_frame_path, front_frame_path, timestamp


def main():
    parser = argparse.ArgumentParser(description="Look up a pipeline config record from deployment_map.csv")
    parser.add_argument("device_id", type=str, help="Device ID to search for")
    parser.add_argument("pipeline_number", type=int, help="Pipeline number to search for")
    parser.add_argument("--password", type=str, required=True, help="SSH password for authentication")
    args = parser.parse_args()

    try:
        with open(DEPLOYMENT_MAP_PATH, newline="", encoding="utf-8") as fh:
            # Skip leading sep= line if present
            first_line = fh.readline()
            if not first_line.lower().startswith("sep="):
                fh.seek(0)
            reader = csv.DictReader(fh)
            for row in reader:
                if row["device_id"] == args.device_id and int(row["pipeline_number"]) == args.pipeline_number:
                    pipeline_config_file_name = row["pipeline_config_file_name"]
                    camera_config_file_name = row["camera_config_file_name"]
                    user = row["user"]
                    host_name = row["host_name"]

                    pipeline_config_path = os.path.join(CONFIGS_PATH, pipeline_config_file_name)
                    camera_config_path = os.path.join(CONFIGS_PATH, camera_config_file_name)

                    print(f"pipeline_config_file_name: {pipeline_config_file_name}")
                    print(f"camera_config_file_name:   {camera_config_file_name}")
                    print(f"device_id:                 {args.device_id}")
                    print(f"pipeline_number:           {args.pipeline_number}")
                    print(f"user:                      {user}")
                    print(f"host_name:                 {host_name}")
                    print(f"pipeline_config_path:      {pipeline_config_path} {'[OK]' if os.path.isfile(pipeline_config_path) else '[NOT FOUND]'}")
                    print(f"camera_config_path:        {camera_config_path} {'[OK]' if os.path.isfile(camera_config_path) else '[NOT FOUND]'}")

                    # Parse fields from pipeline config; if missing, ask user one by one.
                    if os.path.isfile(pipeline_config_path):
                        with open(pipeline_config_path, "r", encoding="utf-8") as pf:
                            pipeline_cfg = yaml.safe_load(pf)
                        io_block = pipeline_cfg.get("io", {})
                        front_url = io_block.get("input_video_path", "")
                        back_url = io_block.get("input_traffic_light_video_path", "")
                        front_camera_ip = urlparse(front_url).hostname or ""
                        back_camera_ip = urlparse(back_url).hostname or ""

                        fixation_block = pipeline_cfg.get("fixation", {})
                        intersection_address = fixation_block.get("intersection_address", "")
                        direction = fixation_block.get("direction", "")
                        gps_coordinates = _load_gps_from_config(fixation_block.get("gps_coordinates", ""))
                    else:
                        print(
                            f"Pipeline config file not found: {pipeline_config_path}",
                            file=sys.stderr,
                        )
                        print("Please provide values that are normally extracted from pipeline config.")
                        front_camera_ip = _prompt_required("front_camera_ip: ")
                        back_camera_ip = _prompt_required("back_camera_ip: ")
                        front_url = f"rtsp://admin:DOM2588205@{front_camera_ip}:554/cam/realmonitor?channel=1&subtype=0"
                        back_url = f"rtsp://admin:DOM2588205@{back_camera_ip}:554/cam/realmonitor?channel=1&subtype=0"
                        intersection_address = _prompt_required("crossroad_name (intersection_address): ")
                        direction = _prompt_required("direction: ")
                        gps_coordinates = _prompt_gps("gps_coordinates: ")

                    intersection_address = _sanitize_ascii_or_prompt(
                        intersection_address,
                        "crossroad_name (intersection_address): ",
                    )
                    direction = _sanitize_ascii_or_prompt(direction, "direction: ")
                    # For GPS: only validate if from user input (not from config)
                    # Prompt if empty, validate the prompted input
                    if not gps_coordinates.strip():
                        gps_coordinates = _prompt_gps("gps_coordinates: ")

                    print(f"front_camera_ip:           {front_camera_ip}")
                    print(f"back_camera_ip:            {back_camera_ip}")
                    print(f"intersection_address:      {intersection_address}")
                    print(f"direction:                 {direction}")
                    print(f"gps_coordinates:           {gps_coordinates}")

                    print(f"Starting SSH tunnels to {host_name} for {user}...")
                    _start_tunnel(host_name, user, args.password, back_camera_ip, args.device_id, args.pipeline_number)
                    _start_second_tunnel(host_name, user, args.password, front_camera_ip, args.device_id, args.pipeline_number)
                    
                    if not _wait_for_tunnel(timeout=30):
                        _stop_tunnel(args.device_id, args.pipeline_number)
                        print("Failed to establish tunnel", file=sys.stderr)
                        sys.exit(1)
                    back_frame_path, front_frame_path, timestamp = _capture_frames_to_folder(
                        back_camera_ip,
                        front_camera_ip,
                        back_url,
                        front_url,
                        CAMERA_FRAME_BASE_PATH,
                        user,
                        host_name,
                    )
                    print(f"back_frame_path:           {back_frame_path}")
                    print(f"front_frame_path:          {front_frame_path}")
                    _stop_tunnel(args.device_id, args.pipeline_number)

                    if not back_frame_path or not front_frame_path:
                        print("Failed to capture one or more frames", file=sys.stderr)
                        sys.exit(1)

                    image_suffix = f"cam_[IP]_{timestamp}.jpg"
                    direction_arg = f"{args.pipeline_number}|{back_camera_ip}|{front_camera_ip}|{direction}"
                    annotation_cmd = [
                        sys.executable,
                        "annotation_tool.py",
                        "--images_path",
                        CAMERA_FRAME_BASE_PATH,
                        "--login",
                        user,
                        "--ip",
                        host_name,
                        "--image_suffix",
                        image_suffix,
                        "--device_id",
                        args.device_id,
                        "--crossroad_name",
                        intersection_address,
                        "--gps",
                        gps_coordinates,
                        direction_arg,
                    ]
                    print(f"annotation_command:        {subprocess.list2cmdline(annotation_cmd)}")
                    annotation_result = subprocess.run(
                        annotation_cmd,
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                    )
                    print(f"annotation_exit_code:      {annotation_result.returncode}")
                    if annotation_result.returncode != 0:
                        sys.exit(annotation_result.returncode)

                    return

    except FileNotFoundError:
        print(f"Error: deployment map not found at {DEPLOYMENT_MAP_PATH}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Deployment map record not found for device_id={args.device_id!r}, pipeline_number={args.pipeline_number}.",
        file=sys.stderr,
    )
    
    # Try to find an existing pipeline for this device_id
    found_user, found_host_name, found_intersection_address, found_gps_coordinates, found_config_path = _find_existing_pipeline(args.device_id, args.pipeline_number)
    
    if found_user:
        print(f"Found existing pipeline for device_id={args.device_id!r}.")
        print(f"Reusing: user={found_user}, host_name={found_host_name}")
        print(f"         intersection_address={found_intersection_address}, gps_coordinates={found_gps_coordinates}")
        print("Please provide only front_camera_ip, back_camera_ip, and direction.")
        
        user = found_user
        host_name = found_host_name
        intersection_address = found_intersection_address
        gps_coordinates = found_gps_coordinates
    else:
        print("Please provide required values one by one.")
        # Values normally taken from deployment map
        user = _prompt_required("user: ")
        host_name = _prompt_required("host_name: ")
        # Values normally extracted from pipeline config
        intersection_address = _prompt_required("crossroad_name (intersection_address): ")
        gps_coordinates = _prompt_gps("gps_coordinates: ")
        
        intersection_address = _sanitize_ascii_or_prompt(
            intersection_address,
            "crossroad_name (intersection_address): ",
        )
        # For GPS: only validate if from user input (not from config)
        if not gps_coordinates.strip():
            gps_coordinates = _prompt_gps("gps_coordinates: ")

    # Always prompt for camera IPs and direction
    front_camera_ip = _prompt_required("front_camera_ip: ")
    back_camera_ip = _prompt_required("back_camera_ip: ")
    front_url = f"rtsp://admin:DOM2588205@{front_camera_ip}:554/cam/realmonitor?channel=1&subtype=0"
    back_url = f"rtsp://admin:DOM2588205@{back_camera_ip}:554/cam/realmonitor?channel=1&subtype=0"
    direction = _prompt_required("direction: ")

    direction = _sanitize_ascii_or_prompt(direction, "direction: ")

    print(f"user:                      {user}")
    print(f"host_name:                 {host_name}")
    print(f"front_camera_ip:           {front_camera_ip}")
    print(f"back_camera_ip:            {back_camera_ip}")
    print(f"intersection_address:      {intersection_address}")
    print(f"direction:                 {direction}")
    print(f"gps_coordinates:           {gps_coordinates}")

    print(f"Starting SSH tunnels to {host_name} for {user}...")
    _start_tunnel(host_name, user, args.password, back_camera_ip, args.device_id, args.pipeline_number)
    _start_second_tunnel(host_name, user, args.password, front_camera_ip, args.device_id, args.pipeline_number)

    if not _wait_for_tunnel(timeout=30):
        _stop_tunnel(args.device_id, args.pipeline_number)
        print("Failed to establish tunnel", file=sys.stderr)
        sys.exit(1)
    back_frame_path, front_frame_path, timestamp = _capture_frames_to_folder(
        back_camera_ip,
        front_camera_ip,
        back_url,
        front_url,
        CAMERA_FRAME_BASE_PATH,
        user,
        host_name,
    )
    print(f"back_frame_path:           {back_frame_path}")
    print(f"front_frame_path:          {front_frame_path}")
    _stop_tunnel(args.device_id, args.pipeline_number)

    if not back_frame_path or not front_frame_path:
        print("Failed to capture one or more frames", file=sys.stderr)
        sys.exit(1)

    image_suffix = f"cam_[IP]_{timestamp}.jpg"
    direction_arg = f"{args.pipeline_number}|{back_camera_ip}|{front_camera_ip}|{direction}"
    annotation_cmd = [
        sys.executable,
        "annotation_tool.py",
        "--images_path",
        CAMERA_FRAME_BASE_PATH,
        "--login",
        user,
        "--ip",
        host_name,
        "--image_suffix",
        image_suffix,
        "--device_id",
        args.device_id,
        "--crossroad_name",
        intersection_address,
        "--gps",
        gps_coordinates,
        direction_arg,
    ]
    print(f"annotation_command:        {subprocess.list2cmdline(annotation_cmd)}")
    annotation_result = subprocess.run(
        annotation_cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    print(f"annotation_exit_code:      {annotation_result.returncode}")
    if annotation_result.returncode != 0:
        sys.exit(annotation_result.returncode)


if __name__ == "__main__":
    main()

