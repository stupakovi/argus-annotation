# argus-annotation

Image annotation tool for Argus traffic monitoring system.

## Installation

```bash
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

## Features

- Annotate road markings (solid lines, stop lines) and traffic lights
- Support for multiple images in a single session
- Automatic generation of camera and pipeline configuration files
- Per-class color-coded annotations

## Usage

### Basic Annotation

```bash
python annotation_tool.py image1.jpg image2.jpg --name output.yaml
```

### Full Example with Pipeline Config Generation

```bash
python annotation_tool.py qora-28/8641.jpg qora-28/8645.jpg \
  --name camera-config-12345678-1.yaml \
  --p_config_name pipeline-config-12345678-1.yaml \
  --front_camera 192.168.100.101 \
  --back_camera 192.168.100.102 \
  --camera_configs camera-config-12345678-1.yaml \
  --device_id 12345678 \
  --crossroad_name "Qora-28 Chorrahasi" \
  --street_name "Amir Temur ko'chasi" \
  --gps "41.2995, 69.2401"
```

## Arguments

### Required
- `images`: One or more image file paths

### Optional
- `--name`: Output YAML filename for annotations (default: `combined.yaml`)
- `--p_config_name`: Pipeline config output filename (triggers config generation)
- `--front_camera`: Front camera IP address (required with `--p_config_name`)
- `--back_camera`: Back camera IP address (required with `--p_config_name`)
- `--camera_configs`: Camera config filename (required with `--p_config_name`)
- `--device_id`: Device ID (required with `--p_config_name`)
- `--crossroad_name`: Crossroad/intersection name (required with `--p_config_name`)
- `--street_name`: Street name/direction (required with `--p_config_name`)
- `--gps`: GPS coordinates (required with `--p_config_name`)

## Annotation Classes

- **Solid line**: Blue line marking
- **Stop line**: White line marking
- **Back stop line**: Magenta line marking
- **Opposite edge**: Orange line marking
- **Opposite middle**: Deep orange line marking
- **Traffic light**: Cyan bounding box

## Controls

- Click a class button, then click 2 points to create annotation
- `r`: Undo last annotation
- `q`: Save and move to next image (or finish if last image)