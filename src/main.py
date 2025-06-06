import gradio as gr
from utils.image_processing import process_images_and_zip
from utils.face_visualization import visualize_face_analysis, create_analysis_animation
import os
import sys
import time
import shutil
import tempfile
import warnings
import re
import uuid
from importlib.metadata import version
import torch
import onnxruntime as ort
import insightface
import imageio
import numpy as np
from cpu_detector import get_cpu_friendly_name, clean_cpu_name_for_ui

# Suppress specific warnings
warnings.filterwarnings("ignore", message="NVIDIA GeForce RTX.*not compatible with the current PyTorch installation")

def custom_warning_filter(message, category, filename, lineno, file=None, line=None):
    if category == UserWarning and re.search(r'NVIDIA GeForce RTX \d+ with CUDA capability sm_\d+ is not compatible', str(message)):
        return None  # Suppress the warning
    return True  # Show other warnings

warnings.showwarning = custom_warning_filter

# Set InsightFace model storage directory to user's home
os.environ["INSIGHTFACE_HOME"] = os.path.join(os.path.expanduser("~"), ".insightface")


backend = None
current_model_name = "buffalo_l"
current_high_res = False

def load_backend(model_name="buffalo_l", high_res=False, device="cpu"):
    """Load the backend model only if needed (lazy loading)."""
    global backend, current_model_name, current_high_res
    if backend is None or current_model_name != model_name or current_high_res != high_res:
        try:
            print(f"Loading model: {model_name} (high_res: {high_res}) on device: {device}...")
            from backend.insightface_backend import InsightFaceBackend
            backend = InsightFaceBackend(model=model_name, high_res=high_res, device=device)
            current_model_name = model_name
            current_high_res = high_res
            print(f"Model {model_name} loaded successfully!")
        except Exception as e:
            print(f"Error loading model {model_name}: {str(e)}")
            backend = None
            raise
    return backend

def validate_inputs(og_images, folder_images):
    """Check if reference and input images are provided."""
    if not og_images and not folder_images:
        return False, "Missing reference and input images"
    if not og_images:
        return False, "Missing reference images"
    if not folder_images:
        return False, "Missing input images"
    return True, ""

def process_and_display(
    og_images, folder_images, min_similarity, top_k, use_avg_embedding, model_name, high_res, device, progress=gr.Progress(track_tqdm=True)
):
    """Main processing function: validates input, runs backend, returns results and status."""
    # REMOVE THIS LINE:
    # torch_device = device.split()[0]  # "cuda:0" or "cpu"
    
    # Use device directly - it's already been processed in on_submit()
    try:
        start_time = time.time()  # Start timing
        current_backend = load_backend(model_name, high_res, device=device)
        total = len(folder_images) if folder_images else 1
        # Show progress bar in Gradio UI
        for idx, _ in enumerate(folder_images or [None]):
            progress((idx + 1) / total, desc=f"Processing images [{idx + 1}/{total}]")
        best_images, zip_path = process_images_and_zip(
            og_images, folder_images, current_backend,
            top_k=top_k, min_similarity=min_similarity, use_avg_embedding=use_avg_embedding
        )
        elapsed = time.time() - start_time  # End timing
        total_uploaded = len(folder_images) if folder_images else 0
        best_count = len(best_images) if best_images else 0
        ref_count = len(og_images) if og_images else 0
        status = (
            f"Model: {model_name} | Reference: {ref_count} images | Uploaded: {total_uploaded} images | "
            f"Best: {best_count} images | Time: {elapsed:.2f} seconds"
        )
        return best_images, zip_path, status
    except ValueError as e:
        error_msg = str(e)
        if "No valid embeddings found" in error_msg:
            return [], None, "No faces were detected in your reference images. Please try different images with clear, visible faces."
        else:
            return [], None, f"Error: {error_msg}"
            
    except Exception as e:
        return [], None, f"Unexpected error: {str(e)}"

def clear_all():
    """Clear all outputs in the UI."""
    return None, None, None, None, None, None, None, None  # Added two more Nones for file uploads

def restart_script():
    """Restart the script (used for the 'Restart Script' button)."""
    python = sys.executable
    os.execl(python, python, *sys.argv)

def clean_gradio_temp():
    temp_dir = tempfile.gettempdir()
    gradio_temp = os.path.join(temp_dir, "gradio")
    if os.path.exists(gradio_temp):
        try:
            shutil.rmtree(gradio_temp)
            return None, None, None, "✅ Gradio temp folder cleaned."
        except Exception as e:
            return None, None, None, f"❌ Failed to clean Gradio temp folder: {e}"
    else:
        return None, None, None, "ℹ️ No Gradio temp folder found."

def get_onnxruntime_status():
    try:
        # Check for each onnxruntime package
        installed_packages = []
        for pkg in ["onnxruntime-gpu", "onnxruntime-directml", "onnxruntime"]:
            try:
                version(pkg)  # This will raise PackageNotFoundError if not installed
                installed_packages.append(pkg)
            except Exception:
                pass
                
        if "onnxruntime-gpu" in installed_packages:
            return "ONNX Runtime: &rarr; <b>NVIDIA GPUs (CUDA)</b>"
        elif "onnxruntime-directml" in installed_packages:
            return "ONNX Runtime: &rarr; <b>AMD/Intel GPUs</b>"
        elif "onnxruntime" in installed_packages:
            return "ONNX Runtime: &rarr; <b>CPU only</b>"
        else:
            return "ONNX Runtime: <b>Not installed</b>"
    except Exception as e:
        return f"ONNX Runtime: <b>Error detecting package</b> ({e})"

def get_cpu_model_name():
    """Get CPU model name across different platforms"""
    import platform
    
    system = platform.system()
    
    try:
        if system == "Windows":
            import subprocess
            result = subprocess.check_output(["wmic", "cpu", "get", "name"], text=True)
            return result.strip().split("\n")[1].strip()
        
        elif system == "Linux":
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
            return "Unknown CPU"
            
        elif system == "Darwin":  # macOS
            import subprocess
            result = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True)
            return result.strip()
            
        else:
            return "Unknown CPU"
    except Exception:
        return "Unknown CPU"

def get_device_choices():
    choices = []
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            name = torch.cuda.get_device_name(i)
            choices.append(f"cuda:{i} ({name})")
    # Try to detect DirectML (AMD/Intel GPU on Windows)
    try:
        if "DmlExecutionProvider" in ort.get_available_providers():
            choices.append("directml *Direct Machine Learning*")
    except ImportError:
        pass
    
    # Get CPU model name - clean it up for UI display
    cpu_name = get_cpu_friendly_name()
    clean_name = clean_cpu_name_for_ui(cpu_name)
    choices.append(f"cpu ({clean_name})")
    
    return choices

def combine_animations(animation_paths, output_path):
    """Combines multiple GIF animations side by side into one"""
    if not animation_paths or len(animation_paths) == 0:
        return None
        
    # If just one animation, return it directly
    if len(animation_paths) == 1:
        return animation_paths[0]
        
    # Load all animations
    animations = []
    for path in animation_paths:
        try:
            frames = imageio.mimread(path)
            animations.append(frames)
        except Exception as e:
            print(f"Error loading animation {path}: {e}")
    
    if not animations:
        return None
    
    # Get the frame count (use the shortest animation)
    min_frames = min(len(anim) for anim in animations)
    
    # Get dimensions
    heights = [anim[0].shape[0] for anim in animations]
    widths = [anim[0].shape[1] for anim in animations]
    max_height = max(heights)
    total_width = sum(widths)
    
    # Create combined frames
    combined_frames = []
    for i in range(min_frames):
        # Create blank canvas
        combined = np.ones((max_height, total_width, 3), dtype=np.uint8) * 255
        
        # Add each animation frame
        x_offset = 0
        for j, anim in enumerate(animations):
            h, w = anim[i].shape[:2]
            # Center vertically
            y_offset = (max_height - h) // 2
            combined[y_offset:y_offset+h, x_offset:x_offset+w] = anim[i]
            x_offset += w
            
        combined_frames.append(combined)
    
    # Save combined animation
    imageio.mimsave(output_path, combined_frames, format='GIF', duration=10.0, loop=0)
    return output_path

def create_ui():
    with gr.Blocks(css="""
        /* Custom Gradio UI styles */
        .gradio-container {
            min-height: 100vh !important;
            padding-bottom: 60px !important;
        }
        .contain {
            min-height: 100% !important;
            display: flex;
            flex-direction: column;
        }
        .gallery-item {
            position: relative;
        }
        .gallery-item .remove-btn {
            position: absolute;
            top: 5px;
            right: 5px;
            background: rgba(255,255,255,0.7);
            border-radius: 50%;
            width: 24px;
            height: 24px;
            line-height: 24px;
            text-align: center;
            cursor: pointer;
            color: #ff0000;
            font-weight: bold;
        }
        #best-images-gallery {
            max-height: none !important;
            overflow-y: visible !important;
            padding-bottom: 40px;
            margin-bottom: 20px;
        }
        .gallery-container {
            min-height: 400px;
            margin-bottom: 40px;
            overflow: visible !important;
        }
        .block {
            padding-bottom: 40px !important;
        }
        .gallery-item img {
            margin-bottom: 15px;
        }
        .slider-container {
            max-width: 200px;
            margin: 5px 0;
        }
        .slider-container .wrap {
            margin: 0 !important;
            padding: 0 !important;
        }
        h2, h3 {
            margin-top: 0.5em;
            margin-bottom: 0.5em;
        }
        .prose {
            margin-bottom: 1em;
        }
        .prose h3 {
            font-size: 1.1em;
            margin-top: 1em;
            margin-bottom: 0.5em;
        }
        #status-message {
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
            text-align: center;
            margin-bottom: 8px;
        }
    """) as ui:
        gr.Markdown("## InsightFace Reference Tool v5")
        gr.Markdown("""
        ### How This Works:
        This app analyzes faces in your images using InsightFace facial recognition technology. 
        Upload reference images of your subject, then upload a folder of images to search through.
        The app will find the most similar matches based on face embeddings.

        *Note: The model will be automatically downloaded if needed. First use may take a few minutes.*
        """)

        # Display GPU type
        gr.Markdown(get_onnxruntime_status()) 

        # Top row: action buttons
        with gr.Row():
            with gr.Column():
                status_message = gr.Markdown("", elem_id="status-message")  # <-- Moved here!
                with gr.Row():
                    clear_btn = gr.Button("Clear")
                    submit_btn = gr.Button("Submit", variant="primary")
                    unload_btn = gr.Button("Restart Script")
                    clean_temp_btn = gr.Button("Clean Gradio Temp")
                    confirm_btn = gr.Button("Confirm Clean", visible=False)

        # Main content: left (inputs) and right (settings/results)
        with gr.Row():
            # Left column: Reference and Input Images
            with gr.Column(scale=1):
                gr.Markdown("### Reference Images")
                ref_image_gallery = gr.Gallery(
                    label="Reference Images", 
                    show_label=False, 
                    elem_id="ref-images-gallery",
                    height=200,
                    columns=4,
                    object_fit="contain"
                )
                ref_image_upload = gr.File(
                    label="Upload Reference Images", 
                    file_count="multiple", 
                    type="filepath",
                    elem_id="ref-image-upload"
                )
                gr.Markdown("### Input Images")
                input_image_upload = gr.File(
                    label="Upload Input Images", 
                    file_count="multiple", 
                    type="filepath",
                    elem_id="input-image-upload",
                    file_types=["image"]
                )
            # Right column: Settings and Results
            with gr.Column(scale=1):
                gr.Markdown("### Settings")
                min_similarity = gr.Slider(
                    label="Similarity", 
                    minimum=0, 
                    maximum=1, 
                    value=0.5, 
                    step=0.01, 
                    interactive=True, 
                    elem_id="min-similarity-slider"
                )
                top_k = gr.Number(
                    label="How many Results (Can return less, if not enough matches)", 
                    value=5, 
                    precision=0,  # Only allow integers
                    interactive=True, 
                    elem_id="top-k-number",
                    info="Enter any positive integer for the number of results to return."
                )
                use_avg_embedding = gr.Checkbox(
                    label="Use Average Embedding", 
                    value=False, 
                    interactive=True, 
                    elem_id="use-avg-embedding-checkbox",
                    info="When enabled, uses the average embedding of all reference images for better matching."
                )
                high_res_mode = gr.Checkbox(
                    label="High-Resolution Mode", 
                    value=False,
                    info="Uses higher resolution detection (768px)"
                )
                show_visualization = gr.Checkbox(
                    label="Show Face Analysis Visualization", 
                    value=True,  # Set to True to enable by default
                    info="Display face detection details (box, landmarks, etc.)"
                )

                device_choices = get_device_choices()
                # Set default device correctly
                default_device = None
                # First try to pick CUDA if available
                for choice in device_choices:
                    if choice.startswith("cuda:"):
                        default_device = choice
                        break
                # If no CUDA device found, pick the CPU option
                if default_device is None:
                    for choice in device_choices:
                        if choice.startswith("cpu"):
                            default_device = choice
                            break
                # Fallback to first available choice if somehow nothing matched
                if default_device is None and device_choices:
                    default_device = device_choices[0]
                
                device_dropdown = gr.Dropdown(
                    choices=device_choices,
                    value=default_device,
                    label="Processing Device",
                    interactive=True,
                    elem_id="device-dropdown"
                )

                gr.Markdown("### Best Matching Images")
                best_image_gallery = gr.Gallery(
                    label="Best Matching Images", 
                    show_label=False, 
                    elem_id="best-images-gallery",
                    height=250,
                    columns=4,
                    object_fit="contain"
                )
                download_link = gr.File(
                    label="Download Best Images ZIP", 
                    file_count="single", 
                    type="filepath",
                    elem_id="download-link"
                )

        # KEEP the complete visualization section (around line 475)
        # Just make sure it has this syntax (no visible=False):
        with gr.Row() as viz_row:
            with gr.Column(scale=1):
                gr.Markdown("### Face Analysis Visualization")
                viz_gallery = gr.Gallery(
                    label="Analysis Steps",
                    show_label=False,
                    columns=3,
                    height=300,
                    object_fit="contain"
                )
                viz_animation = gr.Image(
                    label="Analysis Animation",
                    type="filepath"
                )

        # UI logic: handlers for toggles, buttons, and uploads
        def on_high_res_toggle(checked):
            return "High-resolution mode enabled." if checked else "High-resolution mode disabled."

        def on_clear():
            return clear_all()

        def on_submit(og_images, folder_images, min_similarity, top_k, use_avg_embedding, 
                     high_res, device, show_viz=False, progress=gr.Progress(track_tqdm=True)):
            # Extract device str as before
            device_str = device.split()[0].lower()
            if "*" in device and "directml" in device.lower():
                device_str = "directml"
            
            # Handle visualization if enabled and we have reference images
            if show_viz and og_images:
                # Create temp directory for visualization outputs
                viz_temp_dir = os.path.join(tempfile.gettempdir(), "insightface_viz")
                os.makedirs(viz_temp_dir, exist_ok=True)
                
                # Load backend for visualization
                viz_backend = load_backend("buffalo_l", high_res, device_str)
                
                # Process all reference images, limited to first 5 for performance
                all_viz_results = []
                animation_paths = []
                max_refs = min(len(og_images), 5)  # Process up to 5 reference images
                
                for i, ref_img in enumerate(og_images[:max_refs]):
                    # Generate visualizations for each reference image
                    viz_results = visualize_face_analysis(ref_img, viz_backend, viz_temp_dir)
                    if viz_results:  # Only add if face was detected
                        # Create animation for this reference
                        anim_path = os.path.join(viz_temp_dir, f"face_analysis_{i}_{uuid.uuid4()}.gif")
                        anim_path = create_analysis_animation(viz_results, anim_path)
                        animation_paths.append(anim_path)
                        
                        # Add to results with filename as caption
                        ref_filename = os.path.basename(ref_img)
                        for img, caption in viz_results:
                            all_viz_results.append((img, f"Ref {i+1}: {caption}"))
        
                # Combine all animations side by side
                combined_path = os.path.join(viz_temp_dir, f"combined_{uuid.uuid4()}.gif")
                combined_path = combine_animations(animation_paths, combined_path)
                
                # Process inputs as usual
                best_images, zip_path, status = process_and_display(
                    og_images, folder_images, min_similarity, top_k, 
                    use_avg_embedding, "buffalo_l", high_res, device_str, progress
                )
                
                return all_viz_results, combined_path, best_images, zip_path, status
            else:
                # Skip visualization
                best_images, zip_path, status = process_and_display(
                    og_images, folder_images, min_similarity, top_k, 
                    use_avg_embedding, "buffalo_l", high_res, device_str, progress
                )
                
                return None, None, best_images, zip_path, status

        high_res_mode.change(on_high_res_toggle, inputs=[high_res_mode], outputs=[status_message], queue=False)
        clear_btn.click(
            on_clear, 
            inputs=[], 
            outputs=[
                viz_gallery, viz_animation, 
                ref_image_gallery, best_image_gallery, download_link, status_message,
                ref_image_upload, input_image_upload  # Add these two components
            ],
            queue=False
)
        unload_btn.click(
            lambda: restart_script(),
            inputs=[],
            outputs=[],
            queue=False
        )
        clean_temp_btn.click(
            lambda: (gr.update(visible=True), "⚠️ This will also clean other applications that uses Gradio Click again to confirm."),
            inputs=[],
            outputs=[confirm_btn, status_message]
        )
        confirm_btn.click(
            lambda: (gr.update(visible=False), clean_gradio_temp()[3]),
            inputs=[],
            outputs=[confirm_btn, status_message]
        )
        submit_btn.click(
            on_submit,
            inputs=[
                ref_image_upload, input_image_upload, min_similarity, 
                top_k, use_avg_embedding, high_res_mode, device_dropdown,
                show_visualization  # Add the new checkbox
            ],
            outputs=[
                viz_gallery, viz_animation,  # Add new outputs
                best_image_gallery, download_link, status_message
            ],
            queue=True  # Enable Gradio progress bar
        )
        ref_image_upload.upload(
            lambda files: (files, None, None, "Reference images uploaded. Ready to process."),
            inputs=[ref_image_upload],
            outputs=[ref_image_gallery, best_image_gallery, download_link, status_message],
            queue=False
        )
        input_image_upload.upload(
            lambda files: (None, None, "Input images uploaded. Ready to process."),
            inputs=[input_image_upload],
            outputs=[best_image_gallery, download_link, status_message],
            queue=False
        )
        # Add handler to show/hide viz_row based on checkbox
        show_visualization.change(
            lambda show: gr.update(visible=show),
            inputs=[show_visualization],
            outputs=[viz_row]
        )
        return ui

if __name__ == "__main__":
    import time

    start_time = time.time()
    print("Starting InsightFace Reference Tool v5.3.2")

    # Example: Import heavy libraries
    t0 = time.time()
    print(f"Imported torch in {time.time() - t0:.2f} seconds")

    t0 = time.time()
    print(f"Imported insightface in {time.time() - t0:.2f} seconds")

    # ...repeat for other heavy imports or model loading...

    t0 = time.time()
    # model = insightface.model_zoo.get_model('buffalo_l')  # Example
    # print(f"Loaded model in {time.time() - t0:.2f} seconds")

    print(f"Total startup time: {time.time() - start_time:.2f} seconds")
    ui = create_ui()
    ui.launch(server_name="127.0.0.1", share=False, inbrowser=True)

