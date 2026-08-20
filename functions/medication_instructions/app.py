import json
import os
import time
import uuid
import boto3
from boto3.dynamodb.conditions import Key
from flask import Flask, Response, request, stream_with_context

app = Flask(__name__)

BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "my-app-chat-sessions")
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE)


# ── DynamoDB session helpers ──────────────────────────────────────────────────

def get_session(session_id: str) -> list:
    """Retrieve conversation history for a session. Returns [] if not found."""
    try:
        result = table.get_item(Key={"session_id": session_id})
        item = result.get("Item")
        if item:
            return item.get("messages", [])
    except Exception as e:
        app.logger.warning(f"DynamoDB get_session failed: {e}")
    return []


def save_session(session_id: str, messages: list) -> None:
    """Persist conversation history with a 24-hour TTL."""
    try:
        ttl_timestamp = int(time.time()) + SESSION_TTL_SECONDS
        table.put_item(Item={
            "session_id": session_id,
            "messages": messages,
            "ttl": ttl_timestamp,
        })
    except Exception as e:
        app.logger.warning(f"DynamoDB save_session failed: {e}")

SYSTEM_PROMPT = """You are UbatJelas, a friendly and knowledgeable medication guide assistant.

Your role is to help patients understand their doctor's prescription in simple, clear language.

LANGUAGE DETECTION:
- Automatically detect the language used in the prescription (Bahasa Malaysia or English).
- Respond in the same language as the prescription. If mixed, prefer Bahasa Malaysia.
- If the prescription is in English, respond in English.
- If the prescription is in Bahasa Malaysia, respond in Bahasa Malaysia.

OUTPUT FORMAT:
For EACH medication found in the prescription, provide a structured guide with exactly these 6 sections using markdown:

---

## [Medication Name] ([Generic Name if different])

### 1. Purpose / Kegunaan
- What this medication is for and how it helps

### 2. Dosage / Dos
- How many tablets/ml/units to take per dose
- Exact dose amount (mg, ml, etc.)

### 3. Timing & Frequency / Masa & Kekerapan
- How many times per day
- When to take it (morning, night, with food, before/after meals, etc.)

### 4. Duration / Tempoh Rawatan
- How many days/weeks to complete the course
- Whether to finish the full course even if feeling better

### 5. Warnings & Precautions / Amaran & Langkah Berjaga-jaga
- Important interactions or contraindications
- What to avoid while taking this medication
- When to contact a doctor immediately

### 6. Common Side Effects / Kesan Sampingan Biasa
- List the most common side effects
- Which side effects require medical attention

---

After ALL medications, always end with this disclaimer section:

---

> ⚠️ **Important Reminder / Peringatan Penting**
> Always consult your doctor or pharmacist if you have any concerns about your medication.
> Sentiasa rujuk doktor atau ahli farmasi anda jika ada sebarang kemusykilan mengenai ubat anda.

IMPORTANT RULES:
- Be accurate and medically sound but use simple, easy-to-understand language
- If a prescription image is unclear or information is missing, state what you can read and note what is unclear
- Never invent dosage information — only state what is clearly in the prescription
- If no prescription content is provided, politely ask the user to upload a photo or type their prescription
- Structure your response clearly with proper markdown formatting"""


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
    }


@app.route("/", methods=["OPTIONS"])
def options():
    response = Response("", status=200)
    for key, value in cors_headers().items():
        response.headers[key] = value
    return response


@app.route("/", methods=["POST"])
def invoke():
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}

    prescription_text = body.get("prescription_text", "").strip()
    file_data = body.get("file_data", "").strip()
    file_mime = body.get("file_mime", "").strip()

    # session_id: caller may pass one to continue a conversation,
    # or we generate a new one for a fresh session.
    session_id = body.get("session_id", "").strip() or str(uuid.uuid4())

    # Require at least one input
    if not prescription_text and not file_data:
        response = Response(
            json.dumps({"error": "Please provide a prescription photo or text."}),
            status=400,
            content_type="application/json",
        )
        for key, value in cors_headers().items():
            response.headers[key] = value
        return response

    # ── Load existing conversation history from DynamoDB ──────────────────────
    history = get_session(session_id)

    # ── Build the new user message ────────────────────────────────────────────
    content = []

    # Add file block if present
    if file_data and file_mime:
        if file_mime.startswith("image/"):
            # Bedrock image block
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": file_mime,
                    "data": file_data,
                },
            })
        else:
            # Bedrock document block
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": file_mime,
                    "data": file_data,
                },
            })

    # Build text prompt
    if prescription_text:
        prompt = f"Please analyse this prescription and provide a full medication guide:\n\n{prescription_text}"
    else:
        prompt = "Please analyse the prescription in the attached image/document and provide a full medication guide."

    content.append({"type": "text", "text": prompt})

    new_user_message = {"role": "user", "content": content}

    # Append new user turn to history
    messages = history + [new_user_message]

    bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }

    # Accumulate the full assistant reply so we can save it to DynamoDB
    # after streaming completes.
    accumulated_reply = []

    def generate():
        try:
            bedrock_response = bedrock.invoke_model_with_response_stream(
                modelId=BEDROCK_MODEL_ID,
                body=json.dumps(bedrock_body),
                contentType="application/json",
                accept="application/json",
            )
            stream = bedrock_response.get("body")
            for event in stream:
                chunk = event.get("chunk")
                if chunk:
                    chunk_data = json.loads(chunk.get("bytes").decode("utf-8"))
                    if chunk_data.get("type") == "content_block_delta":
                        delta = chunk_data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            accumulated_reply.append(text)
                            yield text

            # ── Stream complete — persist updated history to DynamoDB ──────────
            full_reply = "".join(accumulated_reply)
            assistant_message = {
                "role": "assistant",
                "content": [{"type": "text", "text": full_reply}],
            }
            # Save user turn + assistant reply (strip base64 blobs from history
            # to keep DynamoDB item size manageable — store text-only user msg)
            text_only_user_message = {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
            updated_history = history + [text_only_user_message, assistant_message]
            save_session(session_id, updated_history)

        except Exception as e:
            yield f"\n\n**Error:** {str(e)}"

    response = Response(
        stream_with_context(generate()),
        content_type="text/plain; charset=utf-8",
    )
    for key, value in cors_headers().items():
        response.headers[key] = value
    # Return the session_id in a response header so the frontend can reuse it
    response.headers["X-Session-Id"] = session_id
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
