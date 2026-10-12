#!/bin/zsh
cd "${0:A:h}"
for py in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
  if command -v "$py" >/dev/null 2>&1; then
    "$py" launch.py "$@" && exit 0
    break
  fi
done
echo
echo "OG360 couldnt start. get python from python.org (it has tkinter) and try again"
read -k 1 "?press any key to close"
