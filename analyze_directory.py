import os
import torchvision
import cv2
import numpy as np
from PIL import Image
import torchvision.transforms as T
import tensorflow as tf
import onnxruntime as ort
from wand.image import Image as WandImage
import pandas as pd

SPECIESCLASSIFIER_PATH = "models/model.onnx"
SPECIESCLASSIFIER_LABELS = "models/labels.txt"

QUALITYCLASSIFIER_PATH = "models/quality.keras"

# prompt user for ONNX inference provider
onnx_provider_input = input("Do you want to use GPU for ONNX inference? (y/n): ").strip().lower()
if onnx_provider_input == 'y':
    ONNX_USE_GPU = True
elif onnx_provider_input == 'n':
    ONNX_USE_GPU = False
if ONNX_USE_GPU:
    ONNX_PROVIDER = ['DmlExecutionProvider']
else:
    ONNX_PROVIDER = ['CPUExecutionProvider']

class maskRCNN:
    def __init__(self):
        self.COCO_INSTANCE_CATEGORY_NAMES = [
            '__background__', 'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus',
            'train', 'truck', 'boat', 'traffic light', 'fire hydrant', 'N/A', 'stop sign',
            'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
            'elephant', 'bear', 'zebra', 'giraffe', 'N/A', 'backpack', 'umbrella', 'N/A', 'N/A',
            'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball',
            'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard', 'tennis racket',
            'bottle', 'N/A', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl',
            'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza',
            'donut', 'cake', 'chair', 'couch', 'potted plant', 'bed', 'N/A', 'dining table',
            'N/A', 'N/A', 'toilet', 'N/A', 'tv', 'laptop', 'mouse', 'remote', 'keyboard', 'cell phone',
            'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'N/A', 'book',
            'clock', 'vase', 'scissors', 'teddy bear', 'hair drier', 'toothbrush'
        ]

        # Initialize the Model
        self.model = torchvision.models.detection.maskrcnn_resnet50_fpn_v2(weights=torchvision.models.detection.MaskRCNN_ResNet50_FPN_V2_Weights.DEFAULT)
        self.model.eval()
    def get_prediction(self,image_data, threshold=0.2):
        """
        Perform Object Detecton on the given image using Mask-RCNN

        Arguments:
            image_data: RGB 3 x height x width numpy array
            threshold: confidence score for detection (default=0.5)        

        Returns:
        tuple: A tuple containing:
            - masks (numpy.ndarray): Binary masks for detected objects.
            - pred_boxes (list): Bounding boxes for detected objects.
                Each box is an array of two tuples, (x1,y1) to (x2,y2)
            - pred_class (list): Class labels for detected objects.
                String values
            - pred_score (list): Confidence scores for detected objects.
        """

        transform = T.Compose([T.ToTensor()])
        img = transform(image_data)
        
        # Perform inference using the pre-trained model
        pred = self.model([img])
        
        # Extract confidence scores from the predictions
        pred_score = list(pred[0]['scores'].detach().numpy())
        # Filter predictions based on the confidence threshold
        if (np.array(pred_score) > threshold).sum()==0:
            return None, None, None, None
        
        pred_t = [pred_score.index(x) for x in pred_score if x > threshold][-1]
        
        # Extract masks, class labels, and bounding boxes for the filtered predictions
        masks = (pred[0]['masks'] > 0.5).squeeze().detach().cpu().numpy()
        
        if len(masks.shape)==2:
            masks = np.expand_dims(masks, axis=0)
        pred_class = [self.COCO_INSTANCE_CATEGORY_NAMES[i] for i in list(pred[0]['labels'].numpy())]
        pred_boxes = [[(i[0], i[1]), (i[2], i[3])] for i in list(pred[0]['boxes'].detach().numpy())]
        
        # Keep only the predictions above the threshold
        masks = masks[:pred_t + 1]
        pred_boxes = pred_boxes[:pred_t + 1]
        pred_class = pred_class[:pred_t + 1]
        
        return masks, pred_boxes, pred_class, pred_score[:pred_t + 1]
    
    def __get_center_of_mass(self,mask):
        # Get the coordinates of the mask
        y, x = np.where(mask > 0)
        # Calculate the center of mass
        center_of_mass = (int(np.mean(x)), int(np.mean(y)))
        return center_of_mass

    def __fsolve(self, func, xmin, xmax):
        # Define the function to find the root of
        def f(x):
            return func(x)

        # Define the range to search for the root
        x_min = xmin
        x_max = xmax

        # Perform binary search
        while x_max - x_min > 10:
            x_mid = (x_min + x_max) / 2
            if f(x_mid) < 0:
                x_min = x_mid
            else:
                x_max = x_mid

        return (x_min + x_max) / 2

    def __get_bounding_box(self,mask):
        # Get center of mass
        center_of_mass = self.__get_center_of_mass(mask)

        # define a mini-function that takes the center of mass and a side length S and returns what fraction of the mask is inside the bounding box
        def get_fraction_inside(center_of_mass, S):
            # Get the bounding box
            x_min = int(center_of_mass[0] - S / 2)
            x_max = int(center_of_mass[0] + S / 2)
            y_min = int(center_of_mass[1] - S / 2)
            y_max = int(center_of_mass[1] + S / 2)

            # Make sure the bounding box is inside the image
            x_min = max(0, x_min)
            x_max = min(mask.shape[1], x_max)
            y_min = max(0, y_min)
            y_max = min(mask.shape[0], y_max)

            # Get the fraction of the mask inside the bounding box
            fraction_inside = np.sum(mask[y_min:y_max, x_min:x_max]) / np.sum(mask)

            return fraction_inside
        
        # Find the side length S such that 80% of the mask is inside the central 60% of the bounding box
        S = self.__fsolve(lambda S: get_fraction_inside(center_of_mass, S) - 0.8, 10, 3000)
        S = int(S*1/0.5)

        # Get the bounding box
        x_min = int(center_of_mass[0] - S / 2)
        x_max = int(center_of_mass[0] + S / 2)
        y_min = int(center_of_mass[1] - S / 2)
        y_max = int(center_of_mass[1] + S / 2)

        # Make sure the bounding box is inside the image
        x_min = max(0, x_min)
        x_max = min(mask.shape[1], x_max)
        y_min = max(0, y_min)
        y_max = min(mask.shape[0], y_max)

        # Make sure the bounding box is square
        SLX = x_max - x_min
        SLY = y_max - y_min

        if(SLX > SLY):
            # get new center of bounding box
            center_of_mass = (int((x_min + x_max) / 2), int((y_min + y_max) / 2))
            # get new side length
            S_new = SLY
            # get new bounding box
            x_min = int(center_of_mass[0] - S_new / 2)
            x_max = int(center_of_mass[0] + S_new / 2)
            y_min = int(center_of_mass[1] - S_new / 2)
            y_max = int(center_of_mass[1] + S_new / 2)
        else:
            # get new center of bounding box
            center_of_mass = (int((x_min + x_max) / 2), int((y_min + y_max) / 2))
            # get new side length
            S_new = SLX
            # get new bounding box
            x_min = int(center_of_mass[0] - S_new / 2)
            x_max = int(center_of_mass[0] + S_new / 2)
            y_min = int(center_of_mass[1] - S_new / 2)
            y_max = int(center_of_mass[1] + S_new / 2)

        return x_min, x_max, y_min, y_max
        
    def get_square_crop(self, mask, img, resize=True):
        """Get a square crop around the mask for quality estimation.
        
        Arugments:
            mask: image mask from get_predictions
            img: the image used for prediction
            resize: bool - whether or not to resize to 1024x1024 (default=True)

        Returns:
            --> square crop around the masked pixels.
        """
        # Get the bounding box
        x_min, x_max, y_min, y_max = self.__get_bounding_box(mask)

        # Get the crop
        crop = img[y_min:y_max, x_min:x_max]
        mask_crop = mask[y_min:y_max, x_min:x_max]

        if resize:
            crop = cv2.resize(crop,(1024,1024))
            mask_crop = cv2.resize(mask_crop.astype(np.uint8),(1024,1024))

        return crop, mask_crop

    def get_species_crop(self, box, img):
        """Get the crop for the bird species classifier.
        
        Arguments:
            box: A bounding box returned by get_prediction.
            img: Image data

        Returns:
            Species classifier cropped image --> numpy image data
        """
        xmin, xmax, ymin, ymax = box[0][0].astype(int), box[1][0].astype(int), box[0][1].astype(int), box[1][1].astype(int)
        species_classifier_crop = img[ymin:ymax, xmin:xmax]

        return species_classifier_crop
    
class BirdSpeciesClassifier:
    def __init__(self, model_path, labels_path):
        self.model_path = model_path
        self.labels_path = labels_path
        with open(labels_path, "r") as f:
            self.labels = [line.strip() for line in f.readlines()]
            self.labels = np.array(self.labels)

        self.session = ort.InferenceSession(self.model_path,providers=ONNX_PROVIDER)
    
    def __preprocess_image(self,image):
        """Preprocess the image data to the model input tensor dimensions."""
        # Convert the image to a float32 numpy array properl sized
        image = cv2.resize(image,dsize=(300,300)).astype(np.float32)
        # Change the channel order from HWC to CHW (channel-first)
        image = np.transpose(image, (2, 0, 1))
        # Add a batch dimension
        image = np.expand_dims(image, axis=0)
        return image
    
    def classify_bird(self, image, top_k=5):
        """Run Bird species Classifier on the image.
        
        Args:
            image: The bird species classifier crop (does not need to be resized)
            top_k: How many of the top predictions to return (default = 5)

        Returns:
            predicted_label: Best prediction
            confidence: Confidence in the prediction
            top_k_labels: Top k predicted labels
            top_k_scores: Top k label confidences.
        """
        # Preprocess the image
        input_tensor = self.__preprocess_image(image)
        # Get the input name for the model
        input_name = self.session.get_inputs()[0].name
        # Run inference
        outputs = self.session.run(None, {input_name: input_tensor})
        # Get the predicted class index
        # Get top 5 classes
        top_k_indices = np.argsort(outputs[0][0])[-top_k:][::-1]
        top_k_scores = outputs[0][0][top_k_indices]
        # Print the top 5 classes and their scores
        predicted_class_index = np.argmax(outputs[0][0])
        # Get the label of the predicted class
        predicted_label = self.labels[predicted_class_index]
        confidence = outputs[0][0][predicted_class_index]
        top_k_labels = self.labels[top_k_indices]
        return predicted_label, confidence, top_k_labels, top_k_scores
    
class QualityClassifier:
    def __init__(self, model_path):
        self.model_path = model_path
        self.model = tf.keras.models.load_model(self.model_path)
    def __preprocess_image_classifier(self, cropped_img, cropped_mask):
        img = cv2.cvtColor(cropped_img, cv2.COLOR_RGB2GRAY)  # shape: (1024, 1024)
        # Take derivative of image using Sobel filter
        sobel_x = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=5)  # shape: (1024, 1024)
        sobel_y = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=5)  # shape: (1024, 1024)
        # Combine derivatives
        img = np.sqrt(sobel_x**2 + sobel_y**2)  # shape: (1024, 1024)
        # Apply mask to image
        img1 = cv2.bitwise_and(img, img, mask=cropped_mask.astype(np.uint8))  # shape: (1024, 1024)
        # Get the other portion of the image
        img2 = cv2.bitwise_and(img, img, mask=(cropped_mask == 0).astype(np.uint8))  # shape: (1024, 1024)
        # Stack the images
        # images = np.dstack((img1, img2))  # shape: (1024, 1024, 2)
        images = np.array([img1]).transpose(1,2,0)  # shape: (1024, 1024, 1) ? 
        return images

    def classify_quality(self, cropped_image, cropped_mask, retry = 5):
        """
        Clsasify the quality of an image use the birds classifier model.
        Output: sigmoidal value between 0 and 1.
        """
        for _ in range(retry):
            try:
                input_data = self.__preprocess_image_classifier(cropped_image, cropped_mask)
                # Predict using the classifier model
                output_value = self.model.predict(np.expand_dims(input_data, axis=0))
                return output_value[0][0]
            except Exception as e:
                print(f"Error during classification: {e}")
        return -1  # Return -1 if classification fails after retries

def read_image(path):
    """Uses ImageMagick to read any input image and returns nparray of image contents in height x width x RGB"""
    # use imagemagick to determine image orientation
    with WandImage(filename=path) as img:
        if img.orientation == 'left_bottom':
            img.rotate(270)
        elif img.orientation == 'right_bottom':
            img.rotate(90)
        elif img.orientation == 'bottom':
            img.rotate(180)
        elif img.orientation == 'top':
            pass  # No rotation needed
        return np.array(img)


def compute_image_similarity_akaze(img1, img2, max_dim=1600):
    if img1 is None or img2 is None:
        return {
            'feature_similarity': -1,
            'feature_confidence': -1,
            'color_similarity': -1,
            'color_confidence': -1,
            'similar': False,
            'confidence': 0
        }
    if img1.shape != img2.shape:
        return {
            'feature_similarity': -1,
            'feature_confidence': -1,
            'color_similarity': -1,
            'color_confidence': -1,
            'similar': False,
            'confidence': 0
        }
    try:
        # Resize for speed
        def resize(img):
            h, w = img.shape[:2]
            scale = max_dim / max(h, w)
            if scale < 1.0:
                img = cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)
            return img
        img1 = resize(img1)
        img2 = resize(img2)

        # Convert to grayscale for AKAZE
        gray1 = cv2.cvtColor(img1, cv2.COLOR_RGB2GRAY) if img1.ndim == 3 else img1
        gray2 = cv2.cvtColor(img2, cv2.COLOR_RGB2GRAY) if img2.ndim == 3 else img2

        akaze = cv2.AKAZE_create()
        kp1, des1 = akaze.detectAndCompute(gray1, None)
        kp2, des2 = akaze.detectAndCompute(gray2, None)

        # Keep best 300 keypoints
        if des1 is not None and len(kp1) > 300:
            kp1, des1 = zip(*sorted(zip(kp1, des1), key=lambda x: x[0].response, reverse=True)[:300])
            kp1 = list(kp1)
            des1 = np.array(des1)

        if des2 is not None and len(kp2) > 300:
            kp2, des2 = zip(*sorted(zip(kp2, des2), key=lambda x: x[0].response, reverse=True)[:300])
            kp2 = list(kp2)
            des2 = np.array(des2)

        # Compute feature confidence as minimum of keypoints detected
        feature_confidence = min(len(kp1), len(kp2)) / 300

        # if feature confidence is low, fall back to color similarity
        if feature_confidence < 0.25 or des1 is None or des2 is None or len(kp1) == 0 or len(kp2) == 0:
            mean1 = np.mean(img1.reshape(-1, img1.shape[-1]), axis=0)
            mean2 = np.mean(img2.reshape(-1, img2.shape[-1]), axis=0)
            color_diff = np.sum(np.abs(mean1 - mean2))
            return {
                'feature_similarity': 0,
                'feature_confidence': 0,
                'color_similarity': color_diff,
                'color_confidence': abs((768 - color_diff) / 768) if color_diff <= 150 else abs(color_diff / 768),
                'similar': color_diff <= 150,
                'confidence': abs((768 - color_diff) / 768) if color_diff <= 150 else abs(color_diff / 768)
            }
        
        # Match features using BFMatcher
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        matches = bf.knnMatch(des1, des2, k=2)
        m_arr = np.array([m.distance for m, n in matches])
        n_arr = np.array([n.distance for m, n in matches])

        # Vectorized Lowe's ratio test
        good_mask = m_arr < 0.7 * n_arr

        # Print number of good matches found
        # Compute feature similarity
        feature_similarity = np.sum(good_mask) / ((len(kp1) + len(kp2)) / 2) if (len(kp1) + len(kp2)) > 0 else 0
        
        similar = feature_similarity >= 0.05
        return {
            'feature_similarity': feature_similarity,
            'feature_confidence': feature_confidence,
            'color_similarity': 0,
            'color_confidence': 0,
            'similar': similar,
            'confidence': feature_confidence
        }
    except Exception as e:
        print(f"Error in compute_image_similarity_akaze: {e}")
        return {
            'feature_similarity': -1,
            'feature_confidence': -1,
            'color_similarity': -1,
            'color_confidence': -1,
            'similar': False,
            'confidence': 0
        }

# Prompt user for input directory.
input_directory = input("Enter the path to the directory containing images: ")
if not os.path.isdir(input_directory):
    print("Invalid directory path. Please try again.")
    exit(1)

# Find all images in the input directory that are RAW files
raw_extensions = [".cr2",".cr3", ".nef", ".arw", ".dng", ".orf", ".raf", ".rw2", ".pef", ".sr2", ".x3f"]
raw_files = [f for f in os.listdir(input_directory) if os.path.isfile(os.path.join(input_directory, f)) and os.path.splitext(f)[1].lower() in raw_extensions]

# if there are no RAW files, find jpeg files instead.
if not raw_files:
    print("No RAW files found. Searching for JPEG files instead.")
    jpeg_extensions = [".jpg", ".jpeg", ".png"]
    raw_files = [f for f in os.listdir(input_directory) if os.path.isfile(os.path.join(input_directory, f)) and os.path.splitext(f)[1].lower() in jpeg_extensions]
# Sort files by name
raw_files.sort()

print(f"Found {len(raw_files)} files in the directory.")

# Prompt user for continue? Y/N
continue_prompt = input("Do you want to continue processing these files? (Y/N): ").strip().lower()
if continue_prompt != 'y':
    print("Exiting without processing files.")
    exit(0)

# Create .kestrel directory.
kestrel_directory = os.path.join(input_directory, ".kestrel")
# Create .kestrel/export, .kestrel/crop directories.
export_directory = os.path.join(kestrel_directory, "export")
crop_directory = os.path.join(kestrel_directory, "crop")
os.makedirs(export_directory, exist_ok=True)
os.makedirs(crop_directory, exist_ok=True)

# Initialize file database.
# This will be a pandas DataFrame to store the results.
#     columns: filename, species, species_confidence,
#              quality, export_path, crop_path, rating

database_name = "kestrel_database.csv"
# First load the database if it exists.
database_path = os.path.join(kestrel_directory, database_name)
if os.path.exists(database_path):
    database = pd.read_csv(database_path)
else:
    # Create a new database
    database = pd.DataFrame(columns=["filename", "species", "species_confidence",
                                     "quality", "export_path", "crop_path", "rating",
                                     "scene_count", "feature_similarity", "feature_confidence", "color_similarity", "color_confidence"])

# Find files that are not in the database.
new_files = [f for f in raw_files if f not in database['filename'].values]
if not new_files:
    print("No new files to process.")
else:
    print(f"Processing {len(new_files)} new files.")

# Prompt user for continue? Y/N
continue_prompt = input("Do you want to continue processing these files? (Y/N): ").strip().lower()
if continue_prompt != 'y':
    print("Exiting without processing files.")
    exit(0)

# Initialize the 3 models.
mask_rcnn = maskRCNN()
species_classifier = BirdSpeciesClassifier(SPECIESCLASSIFIER_PATH, SPECIESCLASSIFIER_LABELS)
quality_classifier = QualityClassifier(QUALITYCLASSIFIER_PATH)

previous_image = None
# Get scene count from the database.
scene_count = database['scene_count'].max() if not database.empty else 0

# Begin processing files.
for raw_file in new_files:
    try:
        print(f"Processing file: {raw_file}")
        # Read the image
        image_path = os.path.join(input_directory, raw_file)
        img = read_image(image_path)
        
        if img is None:
            print(f"Failed to read image: {image_path}. Skipping.")

            # Save a default entry in the database for this file.
            new_entry = {
                "filename": raw_file,
                "species": "Failed to Read",
                "species_confidence": 0,
                "quality": -1,
                "export_path": "N/A",
                "crop_path": "N/A",
                "scene_count": scene_count,
                "rating": 0 ,
                "feature_similarity": -1,
                "feature_confidence": -1,
                "color_similarity": -1,
                "color_confidence": -1
            }
            # Append the new entry to the database. This is a pandas dataframe.
            database = pd.concat([database, pd.DataFrame([new_entry])], ignore_index=True)
            database.to_csv(database_path, index=False, float_format='%.16f')
            continue

        similarity = compute_image_similarity_akaze(previous_image, img)
        if not similarity['similar']:
            scene_count += 1
        
        # Get predictions from Mask-RCNN
        masks, pred_boxes, pred_class, pred_score = mask_rcnn.get_prediction(img)
        if masks is None or pred_boxes is None or pred_class is None or pred_score is None:
            print(f"No valid predictions found in {raw_file}. Skipping.")
            # Save a default entry in the database for this file.
            new_entry = {
                "filename": raw_file,
                "species": "No Bird",
                "species_confidence": 0,
                "quality": -1,
                "export_path": "N/A",
                "crop_path": "N/A",
                "scene_count": scene_count,
                "rating": 0 ,
                "feature_similarity": similarity['feature_similarity'],
                "feature_confidence": similarity['feature_confidence'],
                "color_similarity": similarity['color_similarity'],
                "color_confidence": similarity['color_confidence']
            }
            # Append the new entry to the database
            database = pd.concat([database, pd.DataFrame([new_entry])], ignore_index=True)
            database.to_csv(database_path, index=False, float_format='%.16f')
            continue

        # Get the index of the all 'bird' predictions
        bird_indices = [i for i, c in enumerate(pred_class) if c == 'bird']

        if not bird_indices:
            print(f"No bird predictions found in {raw_file}. Skipping.")
            
            # Save the export file
            export_path = os.path.join(export_directory, f"{os.path.splitext(raw_file)[0]}_export.jpg")
            
            img = cv2.resize(img, (1200, int(1200 * img.shape[0] / img.shape[1])))  # Resize to max dimension of 1200
            cv2.imwrite(export_path, cv2.cvtColor(img,cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 70])  # Convert RGB to BGR for OpenCV

            # save the crop file as a blank image
            crop_path = os.path.join(crop_directory, f"{os.path.splitext(raw_file)[0]}_crop.jpg")

            blank_crop = np.zeros((1024, 1024, 3), dtype=np.uint8)
            cv2.imwrite(crop_path, cv2.cvtColor(blank_crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])  # Convert RGB to BGR for OpenCV

            new_entry = {
                "filename": raw_file,
                "species": "No Bird",
                "species_confidence": 0,
                "quality": -1,
                "export_path": export_path,
                "crop_path": crop_path,
                "scene_count": scene_count,
                "rating": 0 ,
                "feature_similarity": similarity['feature_similarity'],
                "feature_confidence": similarity['feature_confidence'],
                "color_similarity": -1,
                "color_confidence": -1
            }

            # Append the new entry to the database
            database = pd.concat([database, pd.DataFrame([new_entry])], ignore_index=True)
            database.to_csv(database_path, index=False, float_format='%.16f')
            continue # Skip to the next file
        
        highest_confidence_index = bird_indices[np.argmax([pred_score[i] for i in bird_indices])]

        # Get the best mask, box, class, and score
        best_mask = masks[highest_confidence_index]
        best_box = pred_boxes[highest_confidence_index]
        best_class = pred_class[highest_confidence_index]
        best_score = pred_score[highest_confidence_index]

        # Get the species crop
        species_crop = mask_rcnn.get_species_crop(best_box, img)

        # Classify the species
        species_label, species_confidence, top_k_labels, top_k_scores = species_classifier.classify_bird(species_crop)

        # Get the quality crop and mask
        quality_crop, quality_mask = mask_rcnn.get_square_crop(best_mask, img, resize=True)

        # Classify the quality
        quality_score = quality_classifier.classify_quality(quality_crop, quality_mask)

        # Save the results to the database
        export_path = os.path.join(export_directory, f"{os.path.splitext(raw_file)[0]}_export.jpg")
        crop_path = os.path.join(crop_directory, f"{os.path.splitext(raw_file)[0]}_crop.jpg")
        # reduce jpeg quality to 85%
        
        # Update the previous image before resizing
        previous_image = img.copy()

        # resize export image to max dimension of 1200
        img = cv2.resize(img, (1200, int(1200 * img.shape[0] / img.shape[1])))  # Resize to max dimension of 1200
        cv2.imwrite(export_path, cv2.cvtColor(img,cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 70])  # Convert RGB to BGR for OpenCV
        cv2.imwrite(crop_path, cv2.cvtColor(quality_crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])  # Convert RGB to BGR for OpenCV

        # Obtain rating value (0-5):
        # <0.15 = 1, <0.3 = 2, <0.6 = 3, <0.9 = 4, >=0.9 = 5
        # If quality_score is -1, set rating to 0.
        rating = 0
        if quality_score == -1:
            rating = 0
        elif quality_score < 0.15:
            rating = 1
        elif quality_score < 0.3:
            rating = 2
        elif quality_score < 0.6:
            rating = 3
        elif quality_score < 0.9:
            rating = 4
        else:
            rating = 5

        new_entry = {
            "filename": raw_file,
            "species": species_label,
            "species_confidence": species_confidence,
            "quality": quality_score,
            "export_path": export_path,
            "crop_path": crop_path,
            "scene_count": scene_count,
            "feature_similarity": similarity['feature_similarity'],
            "feature_confidence": similarity['feature_confidence'],
            "rating": rating,
            "color_similarity": similarity['color_similarity'],
            "color_confidence": similarity['color_confidence']
        }
        # Append the new entry to the database
        database = pd.concat([database, pd.DataFrame([new_entry])], ignore_index=True)
        database.to_csv(database_path, index=False, float_format='%.16f')
        print(f"Processed {raw_file}: Species: {species_label}, Confidence: {species_confidence}, Quality: {quality_score}, Rating: {rating}, Similarity: {similarity['similar']}, Scene Count: {scene_count}")
        print(f"Similarity - Feature: {similarity['feature_similarity']}, Color: {similarity['color_similarity']}, Confidence: {similarity['confidence']}")
        # Save the database

    except Exception as e:
        print(f"Error reading image {raw_file}: {e}. Skipping.")
        # Save a default entry in the database for this file.
        new_entry = {
            "filename": raw_file,
            "species": "No Bird",
            "species_confidence": 0,
            "quality": -1,
            "export_path": "N/A",
            "crop_path": "N/A",
            "scene_count": scene_count,
            "rating": 0 ,
            "feature_similarity": -1,
            "feature_confidence": -1,
            "color_similarity": -1,
            "color_confidence": -1
        }
        # Append the new entry to the database
        database = pd.concat([database, pd.DataFrame([new_entry])], ignore_index=True)
        # save as csv with very high precision
        database.to_csv(database_path, index=False, float_format='%.16f')
        continue