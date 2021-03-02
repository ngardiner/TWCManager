PROGRAM=/usr/bin/python3.5
PIDFILE=/etc/twcmanager/TWCManager.pid
TWCMANAGER_PATH=/home/pi/TWCManager

while true
do

if [ -f $PIDFILE ]; then
  read PID <$PIDFILE
  echo $PID 
  if [ -d /proc/$PID ] && [ "$(readlink -f /proc/$PID/exe)" = "$PROGRAM" ]; then
    echo "done."
  else
    echo "PID not found, Starting..."
    screen -dm -S TWCManager $TWCMANAGER_PATH/TWCManager.py
  fi
else
    echo "PID file not found "; echo $PIDFILE; echo ", Starting..."
    screen -dm -S TWCManager $TWCMANAGER_PATH/TWCManager.py
fi
sleep 30
done


