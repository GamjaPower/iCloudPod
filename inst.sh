#!/bin/bash 

pip install requests tqdm

# copy inst.json to downloader.py directory
# if inst.json not eists, exit with error
if [ ! -f inst.json ]; then
    echo "inst.json not found!"
    echo "Please make sure inst.json is in the same directory as inst.sh"
    exit 1
fi

python downloader.py  

cat downloads/inst.tar.gz.part* > downloads/inst.tar.gz

rm downloads/inst.tar.gz.part*

tar -xzvf downloads/inst.tar.gz -C downloads
rm downloads/inst.tar.gz

cd downloads/inst
tar -zxvf v0.3.65.tar.gz -C /home
mv -f /home/ComfyUI-0.3.65 /home/ComfyUI
mv -f ComfyUI-Manager /home/ComfyUI/custom_nodes

SOURCE_DIR="models"
TARGET_DIR="/home/ComfyUI/models"

find "$SOURCE_DIR" -type f -print0 | while IFS= read -r -d '' path; do
  base="${path#"$SOURCE_DIR/"}" 
  dest="$TARGET_DIR/$base"
  mv "$path" "$dest"
done

cd /home/ComfyUI


python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd /home/ComfyUI/
python main.py --listen 0.0.0.0

