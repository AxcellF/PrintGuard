from typing import Dict, Optional
import requests
from ...models import (FileInfo, JobInfoResponse,
                       TemperatureReadings, TemperatureReading,
                       PrinterState, PrinterTemperatures, Progress)


class MoonrakerClient:
    """
    A client for interacting with Moonraker's HTTP API.
    
    This class provides methods to control and monitor 3D printers through
    Moonraker's web interface, mirroring the functionality of OctoPrintClient
    but adapted for Moonraker/Klipper endpoints.
    """
    
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        """
        Initialize the Moonraker client.
        
        Args:
            base_url (str): The base URL of the Moonraker instance (e.g., 'http://192.168.1.5')
            api_key (str, optional): The API key for authentication, if configured.
                                   Moonraker often authorizes by IP, so this may not be needed.
        """
        self.base_url = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["X-Api-Key"] = api_key

    def get_job_info(self) -> JobInfoResponse:
        """
        Retrieve information about the current print job.
        
        Uses /printer/objects/query to fetch print_stats and virtual_sdcard status.
        Maps Moonraker state to OctoPrint-like state strings where possible.
        """
        # Query for print_stats and virtual_sdcard to get state and file info
        url = f"{self.base_url}/printer/objects/query?print_stats&virtual_sdcard&display_status"
        resp = requests.get(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        result = data.get("result", {}).get("status", {})
        print_stats = result.get("print_stats", {})
        virtual_sdcard = result.get("virtual_sdcard", {})
        display_status = result.get("display_status", {})
        
        # Map Klipper state to something broadly compatible or use raw string
        # Klipper states: "standing", "printing", "paused", "error", "complete"
        # We ideally want headers that our frontend understands or just pass it through.
        # OctoPrint usually returns "Printing", "Operational", etc.
        klipper_state = print_stats.get("state", "unknown")
        
        state_map = {
            "printing": "Printing",
            "paused": "Paused",
            "complete": "Operational", # Or "Finishing"? "Operational" is idle-ish in OctoPrint
            "standby": "Operational",
            "error": "Error",
            "cancelled": "Operational" # Cancellation usually returns to standby
        }
        
        normalized_state = state_map.get(klipper_state, klipper_state.capitalize())
        
        # Construct FileInfo
        # Moonraker usually provides the filename in print_stats['filename']
        filename = print_stats.get("filename")
        file_info = FileInfo(name=filename) if filename else FileInfo()
        
        # Construct Progress
        # completion is usually display_status.progress (0.0 - 1.0)
        completion = display_status.get("progress", 0.0)
        
        # file_position
        filepos = virtual_sdcard.get("file_position")
        
        # Time info
        print_time = print_stats.get("total_duration") # Time since start
        # Estimating left time is harder without metadata, but display_status might not have it directly
        # Sometimes available in webhooks, but here we just take what we have.
        
        progress = Progress(
            completion=completion,
            filepos=filepos,
            printTime=int(print_time) if print_time is not None else 0
        )
        
        return JobInfoResponse(
            job={"file": file_info},
            progress=progress,
            state=normalized_state
        )

    def cancel_job(self) -> None:
        """
        Cancel the currently running print job.
        Endpoint: POST /printer/print/cancel
        """
        resp = requests.post(
            f"{self.base_url}/printer/print/cancel",
            headers=self.headers,
            timeout=10
        )
        # Moonraker might return success even if idle, so we check status
        resp.raise_for_status()

    def pause_job(self) -> None:
        """
        Pause the currently running print job.
        Endpoint: POST /printer/print/pause
        """
        resp = requests.post(
            f"{self.base_url}/printer/print/pause",
            headers=self.headers,
            timeout=10
        )
        resp.raise_for_status()

    def result_to_temp_reading(self, key: str, data: dict) -> Optional[TemperatureReading]:
        """Helper to extract temp reading from status object"""
        component = data.get(key)
        if not component:
            return None
        return TemperatureReading(
            actual=component.get("temperature", 0.0),
            target=component.get("target", 0.0),
            offset=0.0 # Not standard in simple query
        )

    def get_printer_temperatures(self) -> Dict[str, TemperatureReading]:
        """
        Retrieve current temperature readings.
        Endpoint: /printer/objects/query?heater_bed&extruder
        """
        # We can query multiple heaters. For simplicity we check extruder and heater_bed
        # If there are multiple extruders, we might need a more dynamic approach, 
        # but PrintGuard assumes 'tool0' and 'bed' mostly.
        url = f"{self.base_url}/printer/objects/query?heater_bed&extruder"
        resp = requests.get(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        
        status = resp.json().get("result", {}).get("status", {})
        
        temps = {}
        
        # Map extruder -> tool0
        extruder = status.get("extruder")
        if extruder:
            temps["tool0"] = TemperatureReading(
                actual=extruder.get("temperature", 0.0),
                target=extruder.get("target", 0.0),
                offset=0.0
            )
            
        # Map heater_bed -> bed
        bed = status.get("heater_bed")
        if bed:
            temps["bed"] = TemperatureReading(
                actual=bed.get("temperature", 0.0),
                target=bed.get("target", 0.0),
                offset=0.0
            ) 
            
        return temps

    def get_printer_state(self) -> PrinterState:
        """
        Get comprehensive printer state.
        Combines job info and temps.
        """
        try:
            temps = self.get_printer_temperatures()
            tool0 = temps.get("tool0")
            bed = temps.get("bed")
            
            printer_temps = PrinterTemperatures(
                nozzle_actual=tool0.actual if tool0 else None,
                nozzle_target=tool0.target if tool0 else None,
                bed_actual=bed.actual if bed else None,
                bed_target=bed.target if bed else None
            )
        except Exception:
            printer_temps = PrinterTemperatures()

        try:
            job_info = self.get_job_info()
        except Exception:
            job_info = None

        return PrinterState(
            jobInfoResponse=job_info,
            temperatureReading=printer_temps
        )
