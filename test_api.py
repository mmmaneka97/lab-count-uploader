import os
from dotenv import load_dotenv
import anthropic

# Load your .env file
load_dotenv()

# Get the API key
api_key = os.environ.get("ANTHROPIC_API_KEY")

print(f"API Key loaded: {api_key[:20]}..." if api_key else "NO API KEY FOUND")

# Try to create a client
try:
    client = anthropic.Anthropic(api_key=api_key)
    print("✓ API key works!")
except Exception as e:
    print(f"✗ API key error: {e}")