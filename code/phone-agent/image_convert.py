import sys

from PIL import Image

with Image.open(sys.stdin.buffer) as image:
    image.thumbnail((768, 20000))
    image.convert("RGB").save(sys.argv[1], format="JPEG", quality=60)
