import threading
import time
import logging
import requests
import cv2
import numpy as np

class MJPEGClient:
    """
    A client for reading MJPEG streams from a URL.
    It runs a background thread to continuously read the stream
    and keeps the latest frame available for retrieval.
    """

    def __init__(self, url):
        self.url = url
        if self.url.startswith("mjpeg+"):
            self.url = self.url[6:]
        
        self.last_frame = None
        self.thread = None
        self.running = False
        self.lock = threading.Lock()
        self.opened = True
        
        # Start the capture thread
        self.start()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.opened = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def release(self):
        self.stop()

    def isOpened(self):
        return self.opened

    def read(self):
        """
        Returns the latest frame, similar to cv2.VideoCapture.read()
        Returns: (ret, frame)
        """
        with self.lock:
            if self.last_frame is not None:
                return True, self.last_frame.copy()
            return False, None

    def _capture_loop(self):
        logging.info(f"Starting MJPEG capture loop for {self.url}")
        retry_delay = 1
        
        while self.running:
            try:
                # Open the stream execution
                with requests.get(self.url, stream=True, timeout=10) as r:
                    if r.status_code != 200:
                        logging.warning(f"MJPEG stream returned status {r.status_code}")
                        time.sleep(retry_delay)
                        continue

                    # Attempt to find boundary from headers
                    boundary = None
                    content_type = r.headers.get('content-type', '')
                    if 'boundary=' in content_type:
                        boundary = content_type.split('boundary=')[1].strip()
                        if boundary.startswith('"') and boundary.endswith('"'):
                            boundary = boundary[1:-1]
                    
                    # Fallback if specific boundary not found (common in some cams)
                    # We'll just look for common JPEG markers usually.
                    # But implementing a robust multipart reader is better.
                    # Implementing a simple byte-buffer reader.
                    
                    bytes_buffer = b''
                    for chunk in r.iter_content(chunk_size=4096):
                        if not self.running:
                            break
                        
                        bytes_buffer += chunk
                        
                        # Look for JPEG start/end markers
                        # FF D8 is start, FF D9 is end
                        a = bytes_buffer.find(b'\xff\xd8')
                        if a != -1:
                            b = bytes_buffer.find(b'\xff\xd9', a)
                            
                            if b != -1:
                                jpg = bytes_buffer[a:b+2]
                                bytes_buffer = bytes_buffer[b+2:]
                                
                                # Decode
                                try:
                                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                                    if frame is not None:
                                        with self.lock:
                                            self.last_frame = frame
                                        # Reset retry delay on success
                                        retry_delay = 1
                                except Exception as e:
                                    logging.debug(f"Frame decode error: {e}")
                                
            except Exception as e:
                logging.error(f"MJPEG connection error: {e}")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30) # Exponential backoff capped at 30s
