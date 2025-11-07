#!/bin/bash
# Absolute path to your project
cd /home/aneesh/Desktop/Code/CCTV/motion || exit

# Activate virtual environment
source venv/bin/activate

# Run your Python script
python motion.py

# (optional) deactivate venv after run
deactivate