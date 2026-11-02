#!/bin/bash

APP_ROOT="${APP_ROOT:-/app}"

# Render runtime configuration from the mounted templates.
rm -f "${APP_ROOT}/conf/service_conf.yaml"
while IFS= read -r line || [[ -n "$line" ]]; do
    eval "echo \"$line\"" >> "${APP_ROOT}/conf/service_conf.yaml"
done < "${APP_ROOT}/conf/service_conf.yaml.template"

rm -f /etc/nginx/conf.d/*.conf
sed "s|\${APP_ROOT}|${APP_ROOT}|g" \
    /etc/nginx/conf.d/app.conf.template > /etc/nginx/conf.d/app.conf

/usr/sbin/nginx

export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/

PY=python3
if [[ -z "$WS" || $WS -lt 1 ]]; then
  WS=1
fi

function task_exe(){
    JEMALLOC_PATH=$(pkg-config --variable=libdir jemalloc)/libjemalloc.so
    while [ 1 -eq 1 ];do
      LD_PRELOAD=$JEMALLOC_PATH $PY rag/svr/task_executor.py $1;
    done
}

for ((i=0;i<WS;i++))
do
  task_exe  $i &
done

while [ 1 -eq 1 ];do
    $PY api/server.py
done

wait;
