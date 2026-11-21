#!/usr/bin/env python3
"""
PeakCut Gradio Space Entry Point
Runs the PeakCut FastAPI backend (port 8765) in a background thread
and launches a Gradio app (port 7860) that embeds the PeakCut web UI.
"""
import os
import sys
import threading
import time
import uvicorn
import gradio as gr
import requests

# Ensure we can import studio modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from studio import config

# ---------------------------------------------------------------------------
# Configuration
BACKEND_HOST = "0.0.0.0"
BACKEND_PORT = int(os.getenv("PEAKCUT_PORT", "8765"))
GRADIO_HOST = "0.0.0.0"
GRADIO_PORT = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
HEALTH_CHECK_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Start PeakCut FastAPI backend in a background daemon thread
def start_backend():
    """Launch uvicorn server for studio.server:app"""
    # Import inside function to avoid circular imports
    from studio.server import app as fastapi_app
    
    config_obj = uvicorn.Config(
        fastapi_app,
        host=BACKEND_HOST,
        port=BACKEND_PORT,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config_obj)
    server.run()

# Start backend thread
backend_thread = threading.Thread(target=start_backend, daemon=True)
backend_thread.start()

# Wait for backend to be ready
def wait_for_backend(timeout=120):
    """Wait until backend responds to /healthz"""
    start = time.time()
    url = f"http://127.0.0.1:{BACKEND_PORT}/healthz"
    while time.time() - start < timeout:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200 and resp.json().get("ok"):
                print(f"[PeakCut] Backend ready at {url}")
                return True
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"Backend did not start within {timeout}s")

wait_for_backend()

# ---------------------------------------------------------------------------
# Gradio App
def create_gradio_app():
    """Create the Gradio interface that embeds PeakCut"""
    
    # Get the public URL for this Space
    space_host = os.getenv("SPACE_HOST", f"127.0.0.1:{GRADIO_PORT}")
    backend_url = f"https://{space_host}" if os.getenv("SPACE_ID") else f"http://127.0.0.1:{BACKEND_PORT}"
    
    # Custom CSS for full-screen iframe
    css = """
    #peakcut-frame {
        width: 100vw !important;
        height: 100vh !important;
        border: none;
        position: fixed;
        top: 0;
        left: 0;
        z-index: 9999;
        background: white;
    }
    .gradio-container {
        max-width: 100vw !important;
        padding: 0 !important;
        margin: 0 !important;
    }
    #health-check { display: none; }
    """
    
    with gr.Blocks(css=css, title="PeakCut", theme=gr.themes.Soft()) as demo:
        # Hidden health check endpoint for UptimeRobot/cron-job.org
        health_state = gr.State(value="healthy")
        
        # Main iframe embedding the PeakCut web UI
        gr.HTML(f"""
        <iframe id="peakcut-frame" 
                src="{backend_url}/app" 
                allow="camera; microphone; clipboard-read; clipboard-write"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads allow-modals allow-popups-to-escape-sandbox">
        </iframe>
        """)
        
        # Health check button (hidden, for external monitoring)
        with gr.Row(visible=False) as health_row:
            health_btn = gr.Button("Health Check", elem_id="health-check")
            health_output = gr.Textbox(label="Status", elem_id="health-output")
        
        def check_health():
            """Health check for UptimeRobot/cron-job.org"""
            try:
                resp = requests.get(f"http://127.0.0.1:{BACKEND_PORT}/healthz", timeout=3)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return "✅ PeakCut backend healthy"
            except Exception as e:
                return f"❌ Backend unhealthy: {e}"
            return "❌ Backend unreachable"
        
        health_btn.click(check_health, outputs=health_output)
        
        # Periodic self-ping to keep Space awake (runs every 30s)
        def keep_alive():
            try:
                requests.get(f"http://127.0.0.1:{BACKEND_PORT}/healthz", timeout=5)
            except Exception:
                pass
            return gr.update()
        
        # Timer to ping backend every 30 seconds (keeps Space from sleeping)
        timer = gr.Timer(value=30, active=True)
        timer.tick(keep_alive, inputs=None, outputs=None)
    
    return demo

# ---------------------------------------------------------------------------
# Main
if __name__ == "__main__":
    print(f"[PeakCut] Starting Gradio Space on port {GRADIO_PORT}")
    print(f"[PeakCut] Backend running on port {BACKEND_PORT}")
    
    demo = create_gradio_app()
    demo.launch(
        server_name=GRADIO_HOST,
        server_port=GRADIO_PORT,
        share=False,
        show_error=True,
        quiet=False,
        favicon_path=None,
    )