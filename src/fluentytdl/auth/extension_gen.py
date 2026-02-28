import json
from pathlib import Path
from typing import Any


class ExtensionGenerator:
    """
    Dynamically generates a temporary browser extension for cookie extraction.
    Currently supports Manifest V3 (Chrome, Edge, etc.).
    """

    def generate(self, output_dir: Path, receiver_port: int, auth_token: str = "") -> None:
        """
        Generates the extension files in the specified directory.

        Args:
            output_dir: The directory where the extension files will be created.
            receiver_port: The port of the local HTTP server to receive cookies.
            auth_token: Authentication token for secure server communication.
        """
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)

        self._create_manifest(output_dir)
        self._create_background_js(output_dir, receiver_port, auth_token)

    def _create_manifest(self, output_dir: Path) -> None:
        """Creates the manifest.json file."""
        manifest: dict[str, Any] = {
            "manifest_version": 3,
            "name": "FluentYTDL Auth Helper",
            "version": "1.0",
            "description": "Temporary extension to extract YouTube authentication cookies for FluentYTDL.",
            "permissions": ["cookies", "notifications"],
            "host_permissions": ["*://*.youtube.com/*", "*://accounts.google.com/*"],
            "background": {"service_worker": "background.js"},
        }

        manifest_path = output_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    def _create_background_js(
        self, output_dir: Path, receiver_port: int, auth_token: str = ""
    ) -> None:
        """Creates the background.js file with the injected receiver port and auth token."""

        # The JavaScript code to be injected
        js_content = f"""
const RECEIVER_URL = "http://127.0.0.1:{receiver_port}/submit_cookies";
const AUTH_TOKEN = "{auth_token}";

// State to prevent multiple submissions in a short burst
let isSubmitting = false;

chrome.cookies.onChanged.addListener(async (changeInfo) => {{
    const cookie = changeInfo.cookie;
    
    // We only care about the LOGIN_INFO cookie on youtube.com, 
    // as its creation/update usually signifies a successful login or session refresh.
    if (cookie.domain.includes("youtube.com") && cookie.name === "LOGIN_INFO" && !changeInfo.removed) {{
        
        if (isSubmitting) return;
        isSubmitting = true;

        console.log("FluentYTDL: LOGIN_INFO detected. Extracting cookies...");

        try {{
            // Extract all cookies for youtube.com and google.com
            const ytCookies = await chrome.cookies.getAll({{ domain: "youtube.com" }});
            const googleCookies = await chrome.cookies.getAll({{ domain: "google.com" }});
            
            // Combine and deduplicate based on name+domain+path (simple concatenation for now)
            const allCookies = [...ytCookies, ...googleCookies];
            
            console.log(`FluentYTDL: Sending ${{allCookies.length}} cookies to receiver...`);

            const response = await fetch(RECEIVER_URL, {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'X-Auth-Token': AUTH_TOKEN
                }},
                body: JSON.stringify(allCookies)
            }});
            
            if (response.ok) {{
                console.log("FluentYTDL: Cookies sent successfully.");
                
                // Show success notification
                chrome.notifications.create('dle-success', {{
                    type: 'basic',
                    iconUrl: 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="green"><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2z"/></svg>'),
                    title: 'FluentYTDL',
                    message: 'Cookie extraction succeeded! This window will close in a moment...',
                    priority: 2
                }});

                // Inject success page into the current tab
                try {{
                    const tabs = await chrome.tabs.query({{ active: true }});
                    if (tabs.length > 0) {{
                        await chrome.tabs.update(tabs[0].id, {{
                            url: 'data:text/html;charset=utf-8,' + encodeURIComponent(`
                                <html>
                                <head><title>FluentYTDL - Success</title></head>
                                <body style="display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#1a1a2e;font-family:system-ui,sans-serif;">
                                    <div style="text-align:center;color:#e0e0e0;">
                                        <div style="font-size:64px;margin-bottom:16px;">&#10004;</div>
                                        <h1 style="color:#4ade80;margin:0 0 8px;">Cookie extraction succeeded</h1>
                                        <p style="color:#999;font-size:14px;">This window will close automatically...</p>
                                    </div>
                                </body>
                                </html>
                            `)
                        }});
                    }}
                }} catch (tabErr) {{
                    console.log("FluentYTDL: Could not update tab:", tabErr);
                }}
            }} else {{
                console.error("FluentYTDL: Server responded with error:", response.status);
                isSubmitting = false;
            }}
            
        }} catch (err) {{
            console.error("FluentYTDL: Error extracting/sending cookies:", err);
            isSubmitting = false; // Allow retry on error
        }}
    }}
}});
"""

        js_path = output_dir / "background.js"
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js_content)
