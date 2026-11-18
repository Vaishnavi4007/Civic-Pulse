
import os
import shutil
from sklearn.model_selection import train_test_split

# Original dataset
source_dir = "dataset"

# Destination folders
output_dir = "dataset_split"

classes = ["potholes", "normal"]

for split in ["train", "validation", "test"]:
    for category in classes:
        os.makedirs(
            os.path.join(output_dir, split, category),
            exist_ok=True
        )

for category in classes:

    folder = os.path.join(source_dir, category)

    images = [
        f for f in os.listdir(folder)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]

    # 70% training, 30% remaining
    train_images, remaining = train_test_split(
        images,
        test_size=0.30,
        random_state=42
    )

    # Split remaining into 15% validation and 15% test
    val_images, test_images = train_test_split(
        remaining,
        test_size=0.50,
        random_state=42
    )

    splits = {
        "train": train_images,
        "validation": val_images,
        "test": test_images
    }

    for split, files in splits.items():
        for filename in files:
            shutil.copy2(
                os.path.join(folder, filename),
                os.path.join(output_dir, split, category, filename)
            )

    print(category)
    print("Training:", len(train_images))
    print("Validation:", len(val_images))
    print("Testing:", len(test_images))

print("\nDataset splitting completed!")
