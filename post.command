#!/bin/bash
# Double-click this on macOS to post your picked front pages.
cd "$(dirname "$0")"
python3 post.py
echo
read -p "Done. Press Enter to close."
