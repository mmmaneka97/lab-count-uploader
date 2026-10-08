from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import os
import base64
import requests
import json
from PIL import Image
from io import BytesIO
import anthropic

app = Flask(__name__)
CORS(app)

# Configuration
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def preprocess_image(image_data):
    """
    Convert image to grayscale, increase contrast, and compress for OCR.
    This reduces file size and token usage for Claude Vision.
    """
    img = Image.open(BytesIO(image_data))

    # Convert to grayscale
    if img.mode != 'L':
        img = img.convert('L')

    # Increase contrast
    from PIL import ImageEnhance
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)

    # Reduce size aggressively to keep file small
    max_dimension = 1200
    if img.width > max_dimension or img.height > max_dimension:
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    # Convert to JPEG with compression (much smaller than PNG for grayscale)
    output = BytesIO()
    img.save(output, format='JPEG', quality=85, optimize=True)
    output.seek(0)
    return output.getvalue()

def extract_data_with_claude(image_data):
    """
    Use Claude Vision API to extract colony count data from the image.
    """
    # Convert to base64
    base64_image = base64.standard_b64encode(image_data).decode('utf-8')

    extraction_prompt = """Analyze this laboratory data sheet with colony counts and extract all data entries.

For each entry, extract:
- Sample name/identifier (e.g., "H2O", "SF3", "440", "SYM 40", etc.)
- Colony count(s) - can be a single number, or a pair like "32/45" (two plates)
- Dilution factor (like -7, -8, or if not visible, use empty string)
- Any other relevant info (replicates, concentrations, etc.)

Return ONLY valid JSON (no markdown, no code blocks) with this exact structure:
{
  "entries": [
    {
      "sample_name": "string",
      "plate_1_count": "number or null",
      "plate_2_count": "number or null",
      "dilution": "string (e.g., '-7') or empty",
      "replicate": "string or null",
      "notes": "string or null"
    }
  ],
  "header_info": "any relevant header text (dates, concentrations, etc.)"
}

Be thorough - extract EVERY entry you can see. If a pair of numbers is shown like "32/45", plate_1_count is 32 and plate_2_count is 45."""

    message = client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": extraction_prompt
                    }
                ],
            }
        ],
    )

    # Handle thinking blocks and text blocks
    response_text = None
    for block in message.content:
        if hasattr(block, 'text'):
            response_text = block.text.strip()
            break

    if not response_text:
        return {"error": "No text response from Claude"}

    # Clean up if there's markdown code blocks
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]
    response_text = response_text.strip()

    try:
        data = json.loads(response_text)
        return data
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse Claude response: {str(e)}", "raw_response": response_text}

def send_to_discord(data, user_email):
    """Send formatted data table to Discord webhook."""
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        return {"success": False, "error": "Discord webhook not configured"}

    entries = data.get("entries", [])
    header_info = data.get("header_info", "")

    # Build table string
    table_lines = []
    table_lines.append(f"**Lab Data - {user_email}**")
    if header_info:
        table_lines.append(f"*{header_info}*")
    table_lines.append("")
    table_lines.append("| Sample | Plate 1 | Plate 2 | Dilution | Notes |")
    table_lines.append("|--------|---------|---------|----------|-------|")

    for entry in entries:
        sample = entry.get("sample_name", "").replace("|", "-")
        p1 = entry.get("plate_1_count") or ""
        p2 = entry.get("plate_2_count") or ""
        dilution = entry.get("dilution", "")
        notes = entry.get("notes", "")

        table_lines.append(f"| {sample} | {p1} | {p2} | {dilution} | {notes} |")

    message_content = "\n".join(table_lines)

    payload = {
        "content": message_content
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return {"success": response.status_code == 204, "status": response.status_code}
    except Exception as e:
        return {"success": False, "error": str(e)}

def send_to_slack(data, user_email):
    """Send formatted data table to Slack webhook."""
    if not SLACK_WEBHOOK_URL:
        return {"success": False, "error": "Slack webhook not configured"}

    entries = data.get("entries", [])
    header_info = data.get("header_info", "")

    # Build table for Slack (using code block for CSV-like format)
    table_lines = []
    table_lines.append("Sample,Plate 1,Plate 2,Dilution,Notes")

    for entry in entries:
        sample = entry.get("sample_name", "").replace(",", ";")
        p1 = entry.get("plate_1_count") or ""
        p2 = entry.get("plate_2_count") or ""
        dilution = entry.get("dilution", "")
        notes = entry.get("notes", "").replace(",", ";")

        table_lines.append(f"{sample},{p1},{p2},{dilution},{notes}")

    csv_content = "\n".join(table_lines)

    payload = {
        "text": f"Lab Data from {user_email}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Lab Data - {user_email}*\n{header_info or ''}\n```{csv_content}```"
                }
            }
        ]
    }

    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        return {"success": response.status_code == 200, "status": response.status_code}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
def extract():
    """Extract data from uploaded image."""
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image provided"}), 400

        image_file = request.files['image']
        image_data = image_file.read()
        
        print(f"Image received: {len(image_data)} bytes")

        # Preprocess image
        print("Preprocessing image...")
        processed_image = preprocess_image(image_data)
        print(f"Preprocessed image: {len(processed_image)} bytes")

        # Extract with Claude
        print("Sending to Claude API...")
        extracted_data = extract_data_with_claude(processed_image)
        print(f"Claude response: {extracted_data}")

        return jsonify(extracted_data)

    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
@app.route('/api/send', methods=['POST'])
def send():
    """Send verified data to Discord/Slack."""
    try:
        payload = request.json
        data = payload.get("data", {})
        user_email = payload.get("email", "unknown@lab.com")
        delivery_method = payload.get("delivery_method", "discord")

        if delivery_method == "discord":
            result = send_to_discord(data, user_email)
        elif delivery_method == "slack":
            result = send_to_slack(data, user_email)
        else:
            result = {"success": False, "error": "Unknown delivery method"}

        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
