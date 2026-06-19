#!/bin/bash


echo "......test started docker image being built......"

if [ ! -f "requirements.txt" ]; then 
  echo "requirements.txt not found"
fi

exec python run.py