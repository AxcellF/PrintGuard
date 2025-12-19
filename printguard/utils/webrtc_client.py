
import asyncio
import logging
import threading
import time
import queue
import json
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaRelay

try:
    import aiohttp
except ImportError:
    aiohttp = None

# Monkeypatch for aiortc/cryptography X509 V1 issue
from aiortc.rtcdtlstransport import RTCDtlsTransport, X509_DIGEST_ALGORITHMS
from cryptography.x509.base import InvalidVersion

_original_validate = RTCDtlsTransport._validate_peer_identity

def _patched_validate_peer_identity(self, remoteParameters):
    try:
        # Try original method first
        certificate = self._ssl.get_peer_certificate(as_cryptography=True)
    except InvalidVersion:
        # Fallback for V1 certificates using pyopenssl directly
        logging.warning("Encountered X509 V1 certificate, falling back to legacy validation.")
        certificate = self._ssl.get_peer_certificate(as_cryptography=False)
        
        if not remoteParameters.fingerprints:
            return

        for fingerprint in remoteParameters.fingerprints:
             # PyOpenSSL digest is simple: certificate.digest("SHA256")
             # It returns b'AA:BB:...'
             algo = fingerprint.algorithm.upper()
             try:
                digest = certificate.digest(algo)
             except Exception:
                continue
             
             digest_str = digest.decode("ascii").replace(":", "").lower()
             expected = fingerprint.value.replace(":", "").lower()
             
             if digest_str == expected:
                 return
        
        logging.error(f"DTLS fingerprint mismatch for {digest_str} vs {expected}")
        from aiortc.rtcdtlstransport import State
        self._set_state(State.FAILED)
        return
    return _original_validate(self, remoteParameters)

RTCDtlsTransport._validate_peer_identity = _patched_validate_peer_identity

class WebRTCClient:
    def __init__(self, url):
        self.url = url
        self.queue = queue.Queue(maxsize=1)
        self.stopped = False
        self.thread = None
        self.loop = None
        self.pc = None
        self.pk = None # peer key/id from server
        self.pc = None
        self.pk = None # peer key/id from server
        self._latest_frame = None
        self._last_frame_time = 0
        self._last_pts = None
        self._timeout_sec = 5.0 # Max time to hold last frame if connection drops
        
        # Start background thread
        self.thread = threading.Thread(target=self._run_thread, daemon=True)
        self.thread.start()

    def _run_thread(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._run())
        except Exception as e:
            logging.error(f"WebRTC thread error: {e}")
        finally:
            if self.loop and self.loop.is_running():
                tasks = asyncio.all_tasks(self.loop)
                for t in tasks: t.cancel()
                self.loop.run_until_complete(self._cleanup())
                self.loop.close()

    async def _cleanup(self):
        if self.pc:
            await self.pc.close()

    async def _run(self):
        if aiohttp is None:
            logging.error("aiohttp is required for WebRTC negotiation.")
            return

        ice_servers = [RTCIceServer(urls="stun:stun.l.google.com:19302")]
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        
        @self.pc.on("track")
        def on_track(track):
            logging.info(f"WebRTC Track received: {track.kind}")
            if track.kind == "video":
                asyncio.ensure_future(self._consume_track(track))

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            logging.info(f"WebRTC DataChannel received: {channel.label}")
            if channel.label == "keepalive":
                @channel.on("message")
                def on_message(message):
                    # Reply pong to keepalive
                    try:
                        channel.send("pong")
                    except Exception:
                        pass

        # Try Custom Signaling first (Prusa style)
        connected = await self._connect_custom()
        if not connected:
            logging.info("Custom signaling failed/not applicable, trying WHEP...")
            await self.pc.close()
            self.pc = RTCPeerConnection()
            @self.pc.on("track")
            def on_track_whep(track):
                logging.info(f"WebRTC Track received: {track.kind}")
                if track.kind == "video":
                    asyncio.ensure_future(self._consume_track(track))
            
            connected = await self._connect_whep()
        
        if not connected:
            logging.error("Failed to connect to WebRTC stream via any known method.")
            return

        # Keep alive loop
        while not self.stopped:
            await asyncio.sleep(1)

    async def _connect_custom(self):
        """Connect using the custom JSON signaling (Server Offer)."""
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Send Request
                payload = {
                    "type": "request",
                    "res": None,
                    "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}],
                    "keepAlive": True
                }
                async with session.post(self.url, json=payload) as resp:
                    if resp.status != 200:
                        return False
                    try:
                        data = await resp.json()
                    except:
                        return False
                    
                    if data.get("type") != "offer":
                        return False
                    
                    self.pk = data.get("id")
                    sdp = data.get("sdp")
                    
                    offer = RTCSessionDescription(sdp=sdp, type="offer")
                    await self.pc.setRemoteDescription(offer)
                    
                    # 2. Create Answer
                    answer = await self.pc.createAnswer()
                    await self.pc.setLocalDescription(answer)
                    
                    # Wait for ICE gathering
                    start_gather = time.time()
                    while self.pc.iceGatheringState != "complete" and time.time() - start_gather < 3:
                        await asyncio.sleep(0.1)
                    
                    # 3. Send Answer
                    answer_payload = {
                        "type": "answer",
                        "id": self.pk,
                        "sdp": self.pc.localDescription.sdp
                    }
                    async with session.post(self.url, json=answer_payload) as resp2:
                        if resp2.status != 200:
                            logging.error(f"Failed to send answer: {resp2.status}")
                            return False
                        
                    return True
        except Exception as e:
            logging.debug(f"Custom signaling error (normal if not this type): {e}")
            return False

    async def _connect_whep(self):
        """Connect using WHEP (Client Offer)."""
        try:
            self.pc.addTransceiver("video", direction="recvonly")
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)

            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": "application/sdp"}
                async with session.post(self.url, data=offer.sdp, headers=headers) as resp:
                    if resp.status not in [200, 201]:
                        logging.error(f"WHEP error {resp.status}")
                        return False
                    answer_sdp = await resp.text()
                    answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
                    await self.pc.setRemoteDescription(answer)
                    return True
        except Exception as e:
            logging.error(f"WHEP connection failed: {e}")
            return False

    async def _consume_track(self, track):
        try:
            while not self.stopped:
                try:
                    frame = await track.recv()
                    # Convert AVFrame to numpy (BGR)
                    img = frame.to_ndarray(format="bgr24")
                    
                    # PTS-Based Deadlock Detection:
                    # Update timestamps ONLY if the content (pts) has advanced.
                    # If server sends duplicate frames (keep-alive) with same PTS, time won't advance.
                    if self._last_pts is None or frame.pts != self._last_pts:
                        self._last_frame_time = time.time()
                        self._last_pts = frame.pts
                    
                    if not self.queue.full():
                        self.queue.put(img)
                    else:
                        try:
                            self.queue.get_nowait()
                            self.queue.put(img)
                        except queue.Empty:
                            pass
                except Exception as e:
                    # Normal during shutdown or track end
                    break
        except Exception:
            pass

    def read(self):
        try:
            frame = self.queue.get(timeout=0.1) 
            self._latest_frame = frame
        except queue.Empty:
            frame = self._latest_frame
        
        # Check staleness regardless of whether we got a fresh frame (queue success)
        # or are using the cached frame. Frame is considered stale if PTS hasn't advanced.
        if frame is not None:
             if time.time() - self._last_frame_time < self._timeout_sec:
                 return True, frame
             else:
                 logging.warning("WebRTC stream timeout: PTS stalled for %.1fs, resetting.",
                                 self._timeout_sec)
                 self.stopped = True # Force stop to ensure cleanup
                 return False, None
        
        return False, None

    def release(self):
        self.stopped = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
    
    def isOpened(self):
        return not self.stopped and self.thread and self.thread.is_alive()
