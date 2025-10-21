#!/bin/bash 

pip install requests tqdm

python downloader.py  

cat downloads/inst.tar.gz.part* > downloads/inst.tar.gz

rm downloads/inst.tar.gz.part*

tar -xzvf downloads/inst.tar.gz -C downloads
rm downloads/inst.tar.gz

cd downloads/inst
tar -zxvf v0.3.65.tar.gz -C /home
mv -f /home/ComfyUI-0.3.65 /home/ComfyUI
mv -f ComfyUI-Manager /home/ComfyUI/custom_nodes

# mv -f models /home/ComfyUI

SOURCE_DIR="models"
TARGET_DIR="/home/ComfyUI/models"

mkdir -p "$TARGET_DIR"

find "$SOURCE_DIR" -type f -print0 | while IFS= read -r -d '' path; do
  base="${path#"$SOURCE_DIR/"}" 
  dest="$TARGET_DIR/$base"

  if [[ -e "$dest" ]]; then
    stamp=$(date +"%Y%m%d_%H%M%S")
    dest="$TARGET_DIR/${base%.*}_$stamp.${base##*.}"
  fi

  mv "$path" "$dest"
done

cd /home/ComfyUI


python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt


cd /home/ComfyUI/
# source .venv/bin/activate
python main.py --listen 0.0.0.0

