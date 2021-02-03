PROGRAM=/usr/bin/python3.5
PIDFILE=/home/pi/TWCManager/TWCManager.pid

while true
do

if [ -f $PIDFILE ]; then
  read PID <$PIDFILE
  echo $PID 
  if [ -d /proc/$PID ] && [ "$(readlink -f /proc/$PID/exe)" = "$PROGRAM" ]; then
    echo "done."
  else
    echo "PID not found, Starting..."
    screen -dm -S TWCManager /home/pi/TWCManager/TWCManager.py
  fi
fi
sleep 30
done


