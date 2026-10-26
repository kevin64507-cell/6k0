"""QR helpers: generate, build special payloads (Wi-Fi, contact), and decode."""
import io

import cv2
import numpy as np
import qrcode
from qrcode.constants import ERROR_CORRECT_M

COLORS = {
    "blacqr_utils.pyk": "#000000",
    "blue": "#1565C0",
    "red": "#C62828",
    "green": "#2E7D32",
    "purple": "#6A1B9A",
    "orange": "#E65100",
}

MAX_TEXT_LENGTH = 1000


def make_qr(data: str, color: str = "black", box_size: int = 10) -> io.BytesIO:
    """Return a PNG image (BytesIO) containing a QR code for `data`."""
    qr = qrcode.QRCode(
        version=None,  # auto-size
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(
        fill_color=COLORS.get(color, COLORS["black"]), back_color="white"
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "qrcode.png"
    return buf


def _escape_wifi(value: str) -> str:
    for ch in ("\\", ";", ",", ":", '"'):
        value = value.replace(ch, "\\" + ch)
    return value


def wifi_payload(ssid: str, password: str | None) -> str:
    """Payload that phones recognise as a Wi-Fi network."""
    if password:
        return f"WIFI:T:WPA;S:{_escape_wifi(ssid)};P:{_escape_wifi(password)};;"
    return f"WIFI:T:nopass;S:{_escape_wifi(ssid)};;"


def vcard_payload(name: str, phone: str | None, email: str | None) -> str:
    """Payload for a contact card (vCard 3.0)."""
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{name}", f"N:{name};;;;"]
    if phone:
        lines.append(f"TEL:{phone}")
    if email:
        lines.append(f"EMAIL:{email}")
    lines.append("END:VCARD")
    return "\n".join(lines)


def decode_qr(image_bytes: bytes) -> str | None:
    """Read a QR code from image bytes. Returns the text or None."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return text or None
