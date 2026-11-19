
import os

# Limit TensorFlow CPU threads
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["TF_NUM_INTRAOP_THREADS"] = "1"
os.environ["TF_NUM_INTEROP_THREADS"] = "1"

import tensorflow as tf
import numpy as np
from pathlib import Path

# Configure TensorFlow threads
tf.config.threading.set_intra_op_parallelism_threads(1)
tf.config.threading.set_inter_op_parallelism_threads(1)

# Model path
BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "pothole_cnn.keras"

# Load trained CNN model
model = tf.keras.models.load_model(MODEL_PATH, compile=False)

print("CNN Model loaded successfully!")


# Function to predict pothole
def predict_pothole(image_path):

    img = tf.keras.utils.load_img(
        image_path,
        target_size=(128, 128)
    )

    img_array = tf.keras.utils.img_to_array(img)
    img_array = img_array / 255.0
    img_array = np.expand_dims(img_array, axis=0)

    prediction = model.predict(img_array, verbose=0)[0][0]

    if prediction >= 0.5:
        label = "Pothole"
        confidence = prediction * 100
    else:
        label = "Normal Road"
        confidence = (1 - prediction) * 100

    print("\nPrediction:", label)
    print("Confidence:", round(float(confidence), 2), "%")

    return label, float(confidence)
