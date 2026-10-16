from subprocess import run
from time import sleep
from os import makedirs, path
from datetime import datetime
from threading import Thread
import queue

class Camera:
    photo_queue = queue.Queue()
    worker_thread = None

    @staticmethod
    def ensureConnection(retryCount=3, delaySeconds=2):
        for attempt in range(1, retryCount + 1):
            try:
                result = run(["gphoto2", "--auto-detect"], capture_output=True, text=True, timeout=5)
                output = result.stdout.strip()
                if "usb:" in output.lower():
                    print("Camera connected.")
                    return True
                else:
                    print(f"No camera detected. Attempt {attempt} of {retryCount}.")
                    if attempt < retryCount:
                        sleep(delaySeconds)
            except Exception as error:
                print(f"Error checking camera: {error}")
                return False
        print("Camera connection failed after retries.")
        return False

    @staticmethod
    def getSettingChoices(label, path):
        try:
            if not Camera.ensureConnection():
                print(f"Camera not connected when querying {label}")
                return []
            result = run(["gphoto2", "--get-config", path], capture_output=True, text=True, timeout=5)
            output = result.stdout
            choices = [line.split(" ", 2)[2] for line in output.splitlines()
                       if line.strip().startswith("Choice:") and len(line.split(" ", 2)) == 3]
            return choices
        except Exception as error:
            print(f"Error reading {label}: {error}")
            return []

    @staticmethod
    def setSetting(path, value):
        result = run(["gphoto2", "--set-config", f"{path}={value}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(result.stderr.strip())
        return True

    @staticmethod
    def releaseViewfinder():
        # Release the camera viewfinder to free up the camera
        try:
            # First, kill all existing gphoto2 processes
            print("[camera] Stopping all gphoto2 processes...")
            kill_result = run(["pkill", "-9", "gphoto2"], capture_output=True, text=True)
            # pkill returns 1 if no processes found, which is okay
            if kill_result.returncode not in [0, 1]:
                print(f"[camera] Warning: pkill returned {kill_result.returncode}: {kill_result.stderr}")
            
            # Wait a moment for processes to fully terminate
            sleep(1)
            
            # Now release the viewfinder
            result = run(["gphoto2", "--set-config", "viewfinder=0"], 
                        capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print("[camera] Viewfinder released.")
                return True
            else:
                print(f"[camera] Warning: Failed to release viewfinder: {result.stderr}")
                return False
        except Exception as e:
            print(f"[camera] Error releasing viewfinder: {e}")
            return False

    @staticmethod
    def get_latest_file_number():
        result = run(["gphoto2", "--list-files"], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to list files: {result.stderr.strip()}")
        lines = result.stdout.splitlines()
        max_num = 0
        for line in lines:
            if line.strip().startswith("#"):
                try:
                    num = int(line.split()[0][1:])
                    if num > max_num:
                        max_num = num
                except ValueError:
                    continue
        return max_num

    @staticmethod
    def getPhotoFolder(base_folder="photos", user="default"):
        folder = path.join(base_folder, user)
        makedirs(folder, exist_ok=True)
        return folder

    @staticmethod
    def capturePhoto(base_folder="photos", max_retries=3, currentid=None, save_folder=None):
        # save_folder overrides base_folder/default (front-panel photos, utils/photos.py)
        if save_folder:
            makedirs(save_folder, exist_ok=True)
        else:
            save_folder = Camera.getPhotoFolder(base_folder)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for attempt in range(1, max_retries + 1):
            capture = run(
                ["gphoto2", "--capture-image-and-download", "--filename", f"{save_folder}/{currentid}_photo_{timestamp}.%C"],
                capture_output=True, text=True
            )
            print(f"[DEBUG] Capture output: {capture.stdout} {capture.stderr}")
            if capture.returncode == 0:
                downloaded_files = [
                    path.basename(line.split("Saving file as")[-1].strip())
                    for line in capture.stdout.splitlines()
                    if "Saving file as" in line
                ]
                run(["gphoto2", "--reset"], capture_output=True, text=True)
                sleep(8)
                return downloaded_files
            elif "Could not claim the USB device" in capture.stderr or "resource busy" in capture.stderr:
                print(f"[RETRY] USB busy, attempt {attempt} of {max_retries}.")
                sleep(5)
                continue
            else:
                raise Exception(f"Capture failed: {capture.stderr.strip()}")
        raise Exception("Failed to capture after retries.")

    @staticmethod
    def enqueuePhotoRequest(base_folder, settings, user="default"):
        folder = Camera.getPhotoFolder(base_folder, user)
        Camera.photo_queue.put((folder, settings))
        Camera.startWorker()

    @staticmethod
    def _processQueue():
        while True:
            save_folder, settings = Camera.photo_queue.get()
            try:
                for label, value in settings.items():
                    if label in settings_map:
                        Camera.setSetting(settings_map[label], value)
                        print(f"[QUEUE] Set {label} = {value}")
                files = Camera.capturePhoto(save_folder)
                print(f"[QUEUE] Captured: {files}")
            except Exception as e:
                print(f"[QUEUE] Error: {e}")
            Camera.photo_queue.task_done()

    @staticmethod
    def startWorker():
        if Camera.worker_thread is None or not Camera.worker_thread.is_alive():
            Camera.worker_thread = Thread(target=Camera._processQueue, daemon=True)
            Camera.worker_thread.start()


# Settings map for known camera paths
settings_map = {
    "Shutter Speed": "/main/capturesettings/shutterspeed",
    "ISO": "/main/imgsettings/iso",
}

# Example usage
if __name__ == "__main__":
    if Camera.ensureConnection():
        print("Listing setting options:\n")
        for label, path_ in settings_map.items():
            options = Camera.getSettingChoices(label, path_)
            print(f"{label} options: {options}")

        # Example photo queue usage
        settings_example = {
            "Shutter Speed": "1/100",
            "ISO": "400"
        }
