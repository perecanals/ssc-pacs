#!/bin/bash
# Set the terminal window/tab title (equivalent to `title` in the .cmd)
printf '\033]0;SSC-PACS SSH Tunnel\007'
IP=XXX.XXX.XXX.XXX
USER='XXX'
echo "Opening SSH tunnel to SSC-PACS (localhost:8043 -> $USER@$IP)..."
echo "Enter your password when prompted."
echo
ssh -N -L 8043:localhost:8043 \
  -o ServerAliveInterval=60 -o ServerAliveCountMax=3 \
  -o PermitLocalCommand=yes \
  -o LocalCommand='printf "\n[OK] Tunnel established - open the PACS here:\n\n   http://localhost:8043\n\n   Leave this window open while you use the PACS. Close it to disconnect.\n\n"' \
  "$USER@$IP"
echo
echo "Tunnel closed."
read -n 1 -s -r -p "Press any key to continue . . ."
echo
