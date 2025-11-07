#!/bin/bash
# Activate the virtual environment
source /home/aneesh/Desktop/Code/CCTV/venv/bin/activate

# Move into the motion directory
cd /home/aneesh/Desktop/Code/CCTV/motion || exit

# Log start time
echo "---- $(date) : Starting motion.py ----" >> /home/aneesh/Desktop/Code/CCTV/motion/motion.log

# Run the Python script
python motion.py >> /home/aneesh/Desktop/Code/CCTV/motion/motion.log 2>&1

# Log end time
echo "---- $(date) : Finished motion.py ----" >> /home/aneesh/Desktop/Code/CCTV/motion/motion.log

# Deactivate venv
deactivate
