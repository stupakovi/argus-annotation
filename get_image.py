import cv2
from matplotlib import pyplot as plt
import plotly.express as px
import os
import argparse

def main():
    parser = argparse.ArgumentParser(description='Capture images from RTSP streams')
    parser.add_argument('--name', type=str, help='Name/path for saving images')
    parser.add_argument('--suffix', type=str, help='Name/path for saving images')
    parser.add_argument('--ports', type=int, nargs='+', 
                        default=[18601, 18602, 18603, 18604, 18605, 18606, 18607, 18608, 18609],
                        help='List of ports to capture from (default: 8641-8648)')
    
    args = parser.parse_args()
    name = args.name
    print(name)
    os.makedirs(name, exist_ok=True)
    
    for i in args.ports:
        print(i)
        try:
            if i == 5125 or i == 5121:
                url = f"rtsp://admin:parol12345@localhost:{i}/cam/realmonitor?channel=1&subtype=0"
            else:
                url = f"rtsp://admin:DOM2588205@localhost:{i}/cam/realmonitor?channel=1&subtype=0"
            stream = cv2.VideoCapture(url)

            ret, frame = stream.read()
            file_name = args.suffix.replace("[PORT]", f"{i%100:02}")
            cv2.imwrite(f"{name}/{file_name}.jpg", frame)
        except Exception as e:
            print(f"Error for port {i}: {e}")

if __name__ == "__main__":
    main()

"""
ssh -p 22 -NT -L 18601:192.168.100.101:554 \
  -L 18602:192.168.100.102:554 \
  -L 18603:192.168.100.103:554 \
  -L 18604:192.168.100.104:554 \
  -L 18605:192.168.100.105:554 \
  -L 18606:192.168.100.106:554 \
  -L 18607:192.168.100.107:554 \
  -L 18608:192.168.100.108:554 \
  -L 18609:192.168.100.109:554 \
  -L 18610:192.168.100.110:554 \
  -L 18611:192.168.100.111:554 \
  -L 18612:192.168.100.112:554 \
  -L 18613:192.168.100.113:554 \
  -L 18614:192.168.100.114:554 \
  -L 18615:192.168.100.115:554 \
  -L 18616:192.168.100.116:554 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  avia-5ariq@172.18.1.234
"""

"""
    python get_image.py --name "C:\\temp\\64bit\\devices\\avia-5ariq@172.18.1.234"  --suffix "cam_192_168_100_1[PORT]_20260216_151721" --ports 18603 18608 \
    python get_image.py --name "C:\\temp\\64bit\\devices\\a-donish-yangishaxar@172.18.2.30"  --suffix "cam_192_168_100_1[PORT]_20260216_151722" --ports 18606 \
    python get_image.py --name "C:\\temp\\64bit\\devices\\a-donish-yangishaxar@172.18.2.30"  --suffix "cam_192_168_100_1[PORT]_20260216_151723" --ports 18606 \
"""