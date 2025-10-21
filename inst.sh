#!/bin/bash 

pip install requests tqdm

python downloader.py  

cat downloads/inst.tar.gz.part* > downloads/inst.tar.gz

tar -xzvf downloads/inst.tar.gz -C downloads

