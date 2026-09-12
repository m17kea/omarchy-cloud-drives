#!/bin/bash
if [[ $1 == state ]]; then
  printf '%s\n' '{"ready":true,"error":"","providers":[{"id":"icloud","configured":false,"mounted":false,"path":"~/Cloud/iCloudDrive"}]}'
else
  exit 3
fi
