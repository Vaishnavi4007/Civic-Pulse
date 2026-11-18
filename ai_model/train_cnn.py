
import os

# Dataset paths
pothole_path = "dataset/potholes"
normal_path = "dataset/normal"

# Count image files
extensions = (".jpg", ".jpeg", ".png", ".webp")

pothole_count = sum(
    1 for f in os.listdir(pothole_path)
    if f.lower().endswith(extensions)
)

normal_count = sum(
    1 for f in os.listdir(normal_path)
    if f.lower().endswith(extensions)
)

print("Pothole Images:", pothole_count)
print("Normal Road Images:", normal_count)
print("Total Images:", pothole_count + normal_count)
