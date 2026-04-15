ssh -N -L 8042:localhost:8042 -L 8043:localhost:8043 -L 4242:localhost:4242 -o ServerAliveInterval=60 -o ServerAliveCountMax=3 perecanals@10.110.128.149
# To kill the tunnel: kill $(lsof -ti :8042 -sTCP:LISTEN)
