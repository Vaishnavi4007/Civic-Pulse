
import os
import time

# Configure CPU usage BEFORE importing TensorFlow
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["TF_NUM_INTRAOP_THREADS"] = "1"
os.environ["TF_NUM_INTEROP_THREADS"] = "1"

import tensorflow as tf
import numpy as np
from pathlib import Path

# Limit TensorFlow CPU threads
tf.config.threading.set_intra_op_parallelism_threads(1)
tf.config.threading.set_inter_op_parallelism_threads(1)

# Model path
BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "pothole_cnn.keras"

# Load model only once
print("DEBUG: Loading CNN model...", flush=True)

model = tf.keras.models.load_model(
    MODEL_PATH,
    compile=False
)

print("CNN Model loaded successfully!", flush=True)


# Function to predict pothole
def predict_pothole(image_path):

    start_time = time.time()

    print("DEBUG: Loading image...", flush=True)

    img = tf.keras.utils.load_img(
        image_path,
        target_size=(128, 128),
        color_mode="rgb"
    )

    img_array = tf.keras.utils.img_to_array(img)
    img_array = img_array.astype("float32") / 255.0
    img_array = np.expand_dims(img_array, axis=0)

    print("DEBUG: Image preprocessing completed", flush=True)

    # Direct model inference
    prediction_start = time.time()

    output = model(img_array, training=False)
    prediction = float(np.asarray(output).reshape(-1)[0])

    print(
        f"DEBUG: CNN inference took {time.time() - prediction_start:.2f} seconds",
        flush=True
    )

    if prediction >= 0.5:
        label = "Pothole"
        confidence = prediction * 100
    else:
        label = "Normal Road"
        confidence = (1 - prediction) * 100

    print("DEBUG: Prediction:", label, flush=True)
    print("DEBUG: Confidence:", round(confidence, 2), "%", flush=True)
    print(
        f"DEBUG: Total prediction time: {time.time() - start_time:.2f} seconds",
        flush=True
    )

    return label, confidence
