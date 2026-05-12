#!/usr/bin/env python3
"""
Image annotation tool with per-class colors (lines and boxes).

- Image is shown at original resolution.
- Class buttons have larger, more readable text.

Usage:
    python annotate.py image.png

Controls:
    - Click class button
    - Click 2 points for line or bounding box
    - r = undo last annotation
    - q = save YAML and quit
"""

import cv2
import numpy as np
import os
import sys
import io
import argparse
import shutil
import types
# import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

# Taller bar for bigger text
BUTTON_BAR_HEIGHT = 80
WINDOW_NAME = "Annotation Tool"

# Colors are in BGR format
CLASSES = [
    {"id": "solid_line",          "label": "Solid line",          "type": "line", "color": (255,   0,   0)},   # blue
    {"id": "stop_line",           "label": "Stop line",           "type": "line", "color": (255, 255, 255)},   # white
    {"id": "backview_stop_line",  "label": "Back stop line",      "type": "line", "color": (255,   0, 255)},   # magenta
    {"id": "opposite_edge",       "label": "Opposite edge",       "type": "line", "color": (  0, 165, 255)},   # orange
    {"id": "opposite_middle",     "label": "Opposite middle",     "type": "line", "color": (  0, 128, 255)},   # deep orange
    {"id": "traffic_light",       "label": "Traffic light",       "type": "box",  "color": (255, 255,   0)},   # cyan
]

# Global state
img = None
canvas = None
base_canvas = None
buttons = []
current_class_idx = None
partial_points = []
mouse_pos: Optional[Tuple[int, int]] = None
annotations = []
image_paths: List[str] = []
annotations_store: Dict[str, List[Dict[str, object]]] = {}
current_image_idx = 0


def _find_last_points(annotations: List[Dict[str, object]], class_id: str) -> Optional[List[Tuple[int, int]]]:
    """
    Return the last annotation points for a given class id.

    Args:
        annotations: Collected annotations.
        class_id: Target class identifier.

    Returns:
        Points list for the last matching annotation or None.
    """
    for ann in reversed(annotations):
        if ann["class"] == class_id:
            return ann["points"]  # type: ignore[return-value]
    return None


def _line_points(annotations: List[Dict[str, object]], class_id: str) -> Optional[List[Tuple[int, int]]]:
    """Extract two-point line annotation for a class if present."""
    pts = _find_last_points(annotations, class_id)
    if pts and len(pts) == 2:
        return [(int(p[0]), int(p[1])) for p in pts]
    return None


def _box_coords(annotations: List[Dict[str, object]], class_id: str) -> Optional[List[int]]:
    """Convert a four-point box annotation into [x1, y1, x2, y2]."""
    pts = _find_last_points(annotations, class_id)
    if pts and len(pts) == 4:
        xs = [int(p[0]) for p in pts]
        ys = [int(p[1]) for p in pts]
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _write_point_pair(fh, indent: int, points: Optional[List[Tuple[int, int]]]) -> None:
    """Write p1/p2 entries only when data exists."""
    if not points:
        return

    space = " " * indent
    fh.write(f"{space}p1:\n")
    fh.write(f"{space}  - {points[0][0]}\n")
    fh.write(f"{space}  - {points[0][1]}\n")
    fh.write(f"{space}p2:\n")
    fh.write(f"{space}  - {points[1][0]}\n")
    fh.write(f"{space}  - {points[1][1]}\n")


def _write_box(fh, indent: int, key: str, box: Optional[Sequence[int]]) -> None:
    """Write a four-value box list only when data exists."""
    if not box:
        return

    space = " " * indent
    fh.write(f"{space}{key}:\n")
    for val in box:
        fh.write(f"{space}  - {int(val)}\n")


def _load_existing_docs(yaml_path: str) -> List[Dict[str, object]]:
    """Load a multi-document YAML; ignore non-dict documents."""
    if not os.path.isfile(yaml_path):
        return []
    docs: List[Dict[str, object]] = []
    try:
        with open(yaml_path, "r") as fh:
            for doc in yaml.safe_load_all(fh):
                if isinstance(doc, dict):
                    cleaned = {k: v for k, v in doc.items() if k not in ("path", "image")}
                    docs.append(cleaned)
    except Exception:
        return []
    return docs


def _merge_annotations(existing: Dict[str, object], new: Dict[str, object]) -> Dict[str, object]:
    """Overlay new annotations onto existing ones by key."""
    merged = dict(existing)
    merged.update(new)
    return merged


def _build_annotation_payload(annotations: List[Dict[str, object]]) -> Dict[str, object]:
    """Convert raw annotations for a single image into the schema payload."""
    stop_line_pts = _line_points(annotations, "stop_line")
    backview_pts = _line_points(annotations, "backview_stop_line")
    opp_edge_pts = _line_points(annotations, "opposite_edge")
    opp_mid_pts = _line_points(annotations, "opposite_middle")
    solid_pts = _line_points(annotations, "solid_line")

    tl_main = _box_coords(annotations, "traffic_light")

    data_block: Dict[str, object] = {}

    if stop_line_pts:
        data_block["stop-line"] = {"p1": list(stop_line_pts[0]), "p2": list(stop_line_pts[1])}
    if backview_pts:
        data_block["backview-stop-line"] = {"p1": list(backview_pts[0]), "p2": list(backview_pts[1])}
    if opp_edge_pts or opp_mid_pts:
        opp_block: Dict[str, object] = {}
        if opp_edge_pts:
            opp_block["edge"] = {"p1": list(opp_edge_pts[0]), "p2": list(opp_edge_pts[1])}
        if opp_mid_pts:
            opp_block["middle"] = {"p1": list(opp_mid_pts[0]), "p2": list(opp_mid_pts[1])}
        if opp_block:
            data_block["opposite-stop-lines"] = opp_block
    if solid_pts:
        data_block["solid-line"] = {"p1": list(solid_pts[0]), "p2": list(solid_pts[1])}

    traffic_block: Dict[str, object] = {}
    if tl_main:
        traffic_block["main"] = list(tl_main)
    if traffic_block:
        data_block["traffic-light"] = traffic_block

    return data_block


def _write_line_block(fh, key: str, block: Dict[str, object], indent: int = 0) -> None:
    """Write a line block with p1/p2 if present."""
    p1 = block.get("p1")
    p2 = block.get("p2")
    if p1 and p2 and len(p1) == 2 and len(p2) == 2:
        space = " " * indent
        fh.write(f"{space}{key}:\n")
        _write_point_pair(fh, indent + 2, [(int(p1[0]), int(p1[1])), (int(p2[0]), int(p2[1]))])
        fh.write("\n")


def _write_opposite_block(fh, block: Dict[str, object], indent: int = 0) -> None:
    """Write opposite-stop-lines block if any of its children exist."""
    edge = block.get("edge")
    middle = block.get("middle")
    has_edge = isinstance(edge, dict) and "p1" in edge and "p2" in edge
    has_middle = isinstance(middle, dict) and "p1" in middle and "p2" in middle
    if not (has_edge or has_middle):
        return
    space = " " * indent
    fh.write(f"{space}opposite-stop-lines:\n")
    if has_edge:
        fh.write(f"{space}  edge:\n")
        _write_point_pair(
            fh,
            indent + 4,
            [
                (int(edge["p1"][0]), int(edge["p1"][1])),
                (int(edge["p2"][0]), int(edge["p2"][1])),
            ],
        )
    if has_middle:
        fh.write(f"{space}  middle:\n")
        _write_point_pair(
            fh,
            indent + 4,
            [
                (int(middle["p1"][0]), int(middle["p1"][1])),
                (int(middle["p2"][0]), int(middle["p2"][1])),
            ],
        )
    fh.write("\n")


def _write_traffic_block(fh, block: Dict[str, object], indent: int = 0) -> None:
    """Write traffic-light block with main and sections if present."""
    main = block.get("main")
    sections = block.get("sections", {}) if isinstance(block.get("sections"), dict) else {}

    has_main = isinstance(main, (list, tuple)) and len(main) == 4
    has_sections = any(
        isinstance(sections.get(color), (list, tuple)) and len(sections.get(color)) == 4
        for color in ("red", "yellow", "green")
    )
    if not (has_main or has_sections):
        return

    space = " " * indent
    fh.write(f"{space}traffic-light:\n")
    if has_main:
        _write_box(fh, indent + 2, "main", main)
    section_written = False
    for color in ("red", "yellow", "green"):
        box = sections.get(color)
        if isinstance(box, (list, tuple)) and len(box) == 4:
            if not section_written:
                fh.write(f"{space}  sections:\n")
                section_written = True
            _write_box(fh, indent + 4, color, box)


def save_yaml(yaml_path: str, images_payload: List[Dict[str, object]]) -> None:
    """
    Write annotations for one or more images to YAML using the provided schema.

    Single output file contains multiple documents, one per image.
    Existing documents are merged by position: for each index i, new labels
    override existing labels in document i; extra documents are appended.
    """
    existing_docs = _load_existing_docs(yaml_path)
    merged_docs: List[Dict[str, object]] = []

    max_len = max(len(existing_docs), len(images_payload))
    for idx in range(max_len):
        base: Dict[str, object] = {}
        if idx < len(existing_docs):
            base = {k: v for k, v in existing_docs[idx].items() if k not in ("path", "image")}
        if idx < len(images_payload):
            base = _merge_annotations(base, images_payload[idx])
        merged_docs.append(base)

    def _write_single_doc(fh, block: Dict[str, object], *, write_direction: bool) -> None:
        # Buffer writes so we can guarantee "direction" (if written) is last.
        buf = io.StringIO()

        if "stop-line" in block:
            _write_line_block(buf, "stop-line", block["stop-line"])  # type: ignore[arg-type]
        if "backview-stop-line" in block:
            _write_line_block(buf, "backview-stop-line", block["backview-stop-line"])  # type: ignore[arg-type]
        if "opposite-stop-lines" in block and isinstance(block["opposite-stop-lines"], dict):
            _write_opposite_block(buf, block["opposite-stop-lines"])  # type: ignore[arg-type]
        if "solid-line" in block:
            _write_line_block(buf, "solid-line", block["solid-line"])  # type: ignore[arg-type]
        if "traffic-light" in block and isinstance(block["traffic-light"], dict):
            _write_traffic_block(buf, block["traffic-light"])  # type: ignore[arg-type]

        if write_direction:
            buf.write('direction: "to"\n')
            buf.write('max-allowed-speed-kmh: 60.0\n')
            buf.write('speed-error-threshold-kmh: 5.0\n')
            buf.write('stop-line-y-offset: 5\n')

        fh.write(buf.getvalue().rstrip() + "\n")

    with open(yaml_path, "w") as fh:
        for idx, block in enumerate(merged_docs):
            if not isinstance(block, dict):
                continue

            is_last_doc = (idx == len(merged_docs) - 1)
            _write_single_doc(fh, block, write_direction=is_last_doc)

            if not is_last_doc:
                fh.write("\n")


def build_base_canvas():
    """Create base canvas with original-size image and button bar."""
    global base_canvas, canvas, buttons

    h, w = img.shape[:2]
    base_canvas = np.zeros((h + BUTTON_BAR_HEIGHT, w, 3), dtype=np.uint8)
    # Image at original resolution
    base_canvas[BUTTON_BAR_HEIGHT:, :, :] = img
    canvas = base_canvas.copy()

    btn_width = w // len(CLASSES)
    buttons.clear()
    for i in range(len(CLASSES)):
        x1 = i * btn_width
        x2 = (i + 1) * btn_width - 1 if i < len(CLASSES) - 1 else w - 1
        buttons.append((x1, 0, x2, BUTTON_BAR_HEIGHT - 1))


def redraw_canvas():
    """Redraw buttons, annotations, and in-progress points."""
    global canvas
    canvas = base_canvas.copy()
    ch, cw = canvas.shape[:2]

    # Draw buttons with bigger, clearer text
    for i, cls in enumerate(CLASSES):
        x1, y1, x2, y2 = buttons[i]
        bg = (80, 80, 80) if i != current_class_idx else (0, 160, 0)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), bg, -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 255, 255), 1)

        text = cls["label"]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.8
        thickness = 2
        (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
        tx = x1 + (x2 - x1 - tw) // 2
        ty = y1 + (y2 - y1 + th) // 2

        # Text shadow for better visibility
        cv2.putText(canvas, text, (tx + 1, ty + 1), font, scale, (0, 0, 0), thickness + 1, cv2.LINE_AA)
        cv2.putText(canvas, text, (tx, ty), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

    # Draw stored annotations in their class color
    for ann in annotations:
        pts = ann["points"]
        cls = next(c for c in CLASSES if c["id"] == ann["class"])
        color = cls["color"]

        if ann["type"] == "line" and len(pts) == 2:
            p1 = (pts[0][0], pts[0][1] + BUTTON_BAR_HEIGHT)
            p2 = (pts[1][0], pts[1][1] + BUTTON_BAR_HEIGHT)
            cv2.line(canvas, p1, p2, color, 2)

        elif ann["type"] == "box" and len(pts) == 4:
            pts2 = [(x, y + BUTTON_BAR_HEIGHT) for (x, y) in pts]
            for j in range(4):
                cv2.line(canvas, pts2[j], pts2[(j + 1) % 4], color, 2)

    # Draw in-progress points
    for (x, y) in partial_points:
        cv2.circle(canvas, (x, y + BUTTON_BAR_HEIGHT), 5, (0, 255, 0), -1)

    # Draw live preview when one point is placed and mouse is over the image
    if len(partial_points) == 1 and mouse_pos is not None and current_class_idx is not None:
        cls = CLASSES[current_class_idx]
        color = cls["color"]
        p1_canvas = (partial_points[0][0], partial_points[0][1] + BUTTON_BAR_HEIGHT)
        mp_canvas = (mouse_pos[0], mouse_pos[1] + BUTTON_BAR_HEIGHT)

        if cls["type"] == "line":
            cv2.line(canvas, p1_canvas, mp_canvas, color, 2)
        else:  # box
            x1, x2 = sorted([partial_points[0][0], mouse_pos[0]])
            y1, y2 = sorted([partial_points[0][1], mouse_pos[1]])
            cv2.rectangle(canvas, (x1, y1 + BUTTON_BAR_HEIGHT), (x2, y2 + BUTTON_BAR_HEIGHT), color, 2)

        cv2.circle(canvas, mp_canvas, 5, (0, 255, 0), -1)

    info = (
        f"Image {current_image_idx + 1}/{len(image_paths)} | "
        "Click class → 2 points | r: undo | q: save current"
    )
    cv2.putText(canvas, info, (10, ch - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)


def mouse_callback(event, x, y, flags, param):
    global current_class_idx, partial_points, annotations, mouse_pos

    # Track mouse position for live preview
    if event == cv2.EVENT_MOUSEMOVE:
        img_y = y - BUTTON_BAR_HEIGHT
        img_x = x
        if img is not None and 0 <= img_x < img.shape[1] and 0 <= img_y < img.shape[0]:
            mouse_pos = (img_x, img_y)
        else:
            mouse_pos = None
        if len(partial_points) == 1:
            redraw_canvas()
            cv2.imshow(WINDOW_NAME, canvas)
        return

    if event != cv2.EVENT_LBUTTONDOWN:
        return

    # Click on button bar => select class
    if y < BUTTON_BAR_HEIGHT:
        for i, (bx1, by1, bx2, by2) in enumerate(buttons):
            if bx1 <= x <= bx2:
                current_class_idx = i
                partial_points = []
                redraw_canvas()
                cv2.imshow(WINDOW_NAME, canvas)
                return

    # Click on image area
    if current_class_idx is None:
        print("Select a class first.")
        return

    img_y = y - BUTTON_BAR_HEIGHT
    img_x = x
    h, w = img.shape[:2]

    # Ignore clicks outside image bounds
    if not (0 <= img_x < w and 0 <= img_y < h):
        return

    partial_points.append((img_x, img_y))

    # After 2 points, finalize annotation
    if len(partial_points) == 2:
        p1, p2 = partial_points
        cls = CLASSES[current_class_idx]

        if cls["type"] == "line":
            points = [p1, p2]
        else:  # box
            x1, x2 = sorted([p1[0], p2[0]])
            y1, y2 = sorted([p1[1], p2[1]])
            points = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

        annotations.append({
            "class": cls["id"],
            "type": cls["type"],
            "points": points,
        })
        partial_points = []

    redraw_canvas()
    cv2.imshow(WINDOW_NAME, canvas)

def _set_image(idx: int) -> bool:
    """Load image at index and rebuild canvas."""
    global img, current_image_idx, annotations, partial_points, current_class_idx, mouse_pos

    if idx < 0 or idx >= len(image_paths):
        return False

    path = image_paths[idx]
    loaded = cv2.imread(path)
    if loaded is None:
        print("Error loading image:", path)
        return False

    current_image_idx = idx
    img = loaded
    annotations = annotations_store[path]
    partial_points = []
    mouse_pos = None
    current_class_idx = None

    build_base_canvas()
    redraw_canvas()

    # Ensure window uses original image size + bar
    h, w = img.shape[:2]
    cv2.resizeWindow(WINDOW_NAME, w, h + BUTTON_BAR_HEIGHT)
    cv2.imshow(WINDOW_NAME, canvas)
    return True


def generate_pipeline_config(p_config_name: str, front_camera: str, back_camera: str, 
                            camera_configs: str, device_id: str, crossroad_name: str, 
                            street_name: str, gps: str):
    """Copy pipeline-config-dummy.yaml and replace all <> variables."""
    dummy_path = "pipeline-config-dummy.yaml"
    if not os.path.isfile(dummy_path):
        print(f"Warning: {dummy_path} not found. Skipping pipeline config generation.")
        return
    
    # Extract monitor number from camera_configs (e.g., "camera-config-51907788-2.yaml" -> "2")
    monitor_number = "1"  # default
    try:
        # Remove extension and split by '-'
        base_name = os.path.splitext(os.path.basename(camera_configs))[0]
        parts = base_name.split('-')
        if parts:
            monitor_number = parts[-1]
    except Exception:
        pass
    
    # Read the dummy file
    with open(dummy_path, "r") as f:
        content = f.read()
    
    # Replace all variables
    content = content.replace("<FRONT_CAMERA>", front_camera)
    content = content.replace("<BACK_CAMERA>", back_camera)
    content = content.replace("<CAMERA_CONFIGS>", camera_configs)
    content = content.replace("<DEVICE_ID>", device_id)
    content = content.replace("<CROSSROAD_NAME>", crossroad_name)
    content = content.replace("<STREET_NAME>", street_name)
    content = content.replace("<GPS>", gps)
    content = content.replace("{MONITOR_NUMBER}", monitor_number)
    
    # Write to new file
    with open(p_config_name, "w") as f:
        f.write(content)
    
    print(f"Pipeline config saved: {p_config_name}")


def _main_internal(args):
    global image_paths, annotations_store, annotations, img

    # parser = argparse.ArgumentParser(description="Image annotation tool")
    # parser.add_argument("images", nargs="+", help="Image file paths")
    # parser.add_argument("--name", default="combined.yaml", help="Output YAML filename (default: combined.yaml)")
    
    # # Pipeline config arguments
    # parser.add_argument("--p_config_name", help="Pipeline config output filename (optional)")
    # parser.add_argument("--front_camera", help="Front camera IP address")
    # parser.add_argument("--back_camera", help="Back camera IP address")
    # parser.add_argument("--camera_configs", help="Camera config file name")
    # parser.add_argument("--device_id", help="Device ID")
    # parser.add_argument("--crossroad_name", help="Crossroad/intersection name")
    # parser.add_argument("--street_name", help="Street name/direction")
    # parser.add_argument("--gps", help="GPS coordinates")
    
    # args = parser.parse_args()

    # Generate pipeline config if requested
    if args.p_config_name:
        required_args = {
            "front_camera": args.front_camera,
            "back_camera": args.back_camera,
            "camera_configs": args.camera_configs,
            "device_id": args.device_id,
            "crossroad_name": args.crossroad_name,
            "street_name": args.street_name,
            "gps": args.gps
        }
        
        missing = [k for k, v in required_args.items() if not v]
        if missing:
            print(f"Error: When using --p_config_name, these arguments are required: {', '.join('--' + k for k in missing)}")
            return
        
        generate_pipeline_config(
            args.p_config_name,
            args.front_camera,
            args.back_camera,
            args.camera_configs,
            args.device_id,
            args.crossroad_name,
            args.street_name,
            args.gps
        )

    image_paths = [p for p in args.images if os.path.isfile(p)]
    if not image_paths:
        print("No valid image paths provided.")
        return

    annotations_store = {p: [] for p in image_paths}
    annotations = annotations_store[image_paths[0]]

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

    if not _set_image(0):
        return
    

    output_dir = "./"
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, args.name)

    while True:
        cv2.imshow(WINDOW_NAME, canvas)
        key = cv2.waitKey(20) & 0xFF

        if key == ord('q'):
            # Save current image annotations and either move to next or write file.
            current_path = image_paths[current_image_idx]
            annotations_store[current_path] = annotations

            if current_image_idx + 1 < len(image_paths):
                if not _set_image(current_image_idx + 1):
                    break
            else:
                # Build payload for all images and write once.
                payload: List[Dict[str, object]] = []
                for pth in image_paths:
                    payload.append(_build_annotation_payload(annotations_store[pth]))

                save_yaml(output_file, payload)
                print("Saved:", output_file)
                break

        elif key == ord('r'):
            if annotations:
                annotations.pop()
                partial_points.clear()
                redraw_canvas()
    
    # load combined.yaml
    
    cv2.destroyAllWindows()

def main():
    parser = argparse.ArgumentParser(description="Image annotation tool")
    parser.add_argument("--images_path", help="Image file paths")
    parser.add_argument("--login", help="Image file paths")
    parser.add_argument("--ip", help="Image file paths")
    parser.add_argument("--directions", help="Image file paths")
    parser.add_argument("--image_suffix", help="Image file paths")
    parser.add_argument("--device_id", help="Image file paths")
    parser.add_argument("--crossroad_name", help="Image file paths")
    parser.add_argument("--gps", help="Image file paths")
    parser.add_argument("directions", nargs="+", help="Image file paths")

    args = parser.parse_args()

    deployment_maps = []
    for direction in args.directions:
        splited_string = direction.split('|')
        if len(splited_string) < 4:
            print(
                "Error: Each direction must be in format "
                "direction_id|back_ip|front_ip|direction_name"
            )
            return

        direction_id = splited_string[0]
        back_ip = splited_string[1]
        front_ip = splited_string[2]
        direction_name = "|".join(splited_string[3:])
        back_image = args.images_path + "\\" + args.login + "@" + args.ip + "\\" + args.image_suffix.replace("[IP]", back_ip.replace(".", "_"))
        front_image = args.images_path + "\\" + args.login + "@" + args.ip + "\\" + args.image_suffix.replace("[IP]", front_ip.replace(".", "_"))

        init_args = types.SimpleNamespace()
        init_args.images = [ back_image, front_image ]
        init_args.name = "camera-config-" + args.device_id + "-" + str(direction_id) + ".yaml"
        init_args.p_config_name = "pipeline-config-" + args.device_id + "-" + str(direction_id) + ".yaml"
        init_args.front_camera = front_ip
        init_args.back_camera = back_ip
        init_args.camera_configs = "camera-config-" + args.device_id + "-" + str(direction_id) + ".yaml"
        init_args.device_id = args.device_id
        init_args.crossroad_name = args.crossroad_name
        init_args.street_name = direction_name
        init_args.gps = args.gps
        
        #pipeline-config-58208121-1.yaml,camera-config-58208121-1.yaml,58208121,kellis-abdujalil,172.18.1.142,1
        deployment_map = ",".join([init_args.p_config_name, init_args.name, init_args.device_id, args.login, args.ip, str(direction_id)])
        _main_internal(init_args)
        deployment_maps.append(deployment_map)

    for deployment_map in deployment_maps:
        print(deployment_map)



if __name__ == "__main__":
    main()



# python viz_tf_light.py --image new5/5442.jpg --yaml combined2.yaml

"""
python annotation_tool.py \
    C:\\temp\\64bit\\devices\\kellis-abdujalil@172.18.1.142\\cam_192_168_100_103_20260216_151723.jpg\
    C:\\temp\\64bit\\devices\\kellis-abdujalil@172.18.1.142\\cam_192_168_100_107_20260216_151723.jpg \
    --name camera-config-58208121-3.yaml \
    --p_config_name pipeline-config-58208121-3.yaml \
    --front_camera 192.168.100.107 \
    --back_camera 192.168.100.103 \
    --camera_configs camera-config-58208121-3.yaml \
    --device_id 58208121 \
    --crossroad_name "Toshkent sh., Olmazor tum., Keles yo‘li–Abdujalil ota ko'ch., kesishmasi" \
    --street_name "Keles yo'li ko'ch(Janubiy- Sharqiy)" \
    --gps "41.378637, 69.220700"
"""

"""
python annotation_tool.py \
    --images_path C:\\temp\\64bit\\devices \
    --login "xirmontepa" \
    --ip 172.18.1.174 \
    --image_suffix "cam_[IP]_20260216_151721.jpg" \
    --device_id 83318378 \
    --crossroad_name "Toshkent sh., Yunusobod tum., A.Donish - Yangi shahar ko'ch., chorrahasi"\
    --gps "41.349374, 69.262908" \
    "192.168.100.103|192.168.100.106|A.Donish ko'ch(Janubiy- G'arbiy) '2'"     \
    "192.168.100.102|192.168.100.105|Yangi shahar ko'ch(Janubiy- Sharqiy)"     \
    "192.168.1.104|192.168.1.107|A.Donish ko'ch(Janubiy- G'arbiy) '1'"     \
    "192.168.1.106|192.168.1.111|A.Donish ko'ch(Shimoliy- Sharqiy)"     
    """