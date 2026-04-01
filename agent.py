import os
import json
import httpx
from dotenv import load_dotenv

# --- LIVEKIT 1.5.1+ CORE ---
from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli, llm, Agent, AgentSession
from livekit.agents import function_tool

# --- PLUGINS ---
from livekit.plugins import deepgram, silero, elevenlabs, groq

# --- YOUR EXTERNAL PROMPT ---
from prompts import SYSTEM_PROMPT
import time
import asyncio

load_dotenv()


# ========================================
# JSON TOOL DEFINITIONS (ASYNC FIX)
# ========================================

def _load_json(filename):
    """Internal helper to read your JSON data safely."""
    try:
        base_path = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_path, filename)
        with open(file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Error loading {filename}: {e}")
        return None


# ADDED 'async' keyword to make these awaitable by the Agent
@function_tool(description="Lookup current room availability, rent prices, and security deposits.")
async def get_room_info(room_type: str = None) -> str:
    """Provides details about room sharing types and costs."""
    data = _load_json("seat_availibility.json")
    if not data: return "Room data is currently unavailable."

    if room_type:
        availability = data.get('availability', [])
        matches = [r for r in availability if room_type.lower() in r.get('type', '').lower()]
        return json.dumps(matches) if matches else f"No rooms matching '{room_type}' found."

    return json.dumps(data)


@function_tool(description="Lookup hostel rules, booking, cancellation, and stay policies.")
async def get_hostel_policies(category: str = "all") -> str:
    """Provides details about hostel regulations and notice periods."""
    data = _load_json("rules.json")
    if not data: return "Policy data is currently unavailable."

    if category != "all" and category in data:
        return json.dumps(data[category])
    return json.dumps(data)


# ========================================
# ASYNC AIRTABLE LOGGING
# ========================================

async def log_to_airtable(caller_number, full_transcript, duration):
    pat = os.getenv("AIRTABLE_PAT")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    table_name = "call_logs"

    if not (pat and base_id):
        print("⚠️ Airtable Error: Missing PAT or Base ID in .env")
        return

    url = f"https://api.airtable.com/v0/{base_id}/{table_name}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json"
    }

    payload = {"records": [{"fields": {
        "caller_number": str(caller_number),
        "duration_seconds": int(duration),
        "transcript": str(full_transcript),
    }}]}

    async with httpx.AsyncClient() as client:
        try:
            print(f"📡 Sending data to Airtable Table: {table_name}...")
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                print(f"✅ AIRTABLE SUCCESS: Logged call from {caller_number}")
            else:
                # THIS LINE IS THE KEY: It will tell us the exact error from Airtable
                print(f"❌ AIRTABLE FAILED ({response.status_code}): {response.text}")

        except Exception as e:
            print(f"❌ CONNECTION ERROR: {e}")


async def safe_upload(ctx, chat_history, start_time):
    duration = int(time.time() - start_time)
    print("Inside safe_upload Method.")
    print(f"DEBUG chat_history length: {len(chat_history)}")
    if not chat_history:
        print("ℹ️ No transcript captured. Skipping Airtable.")
        return

    # Extract number: call-_+919861579417_2J... -> +919861579417
    parts = ctx.room.name.split('_')
    caller_number = parts[1] if len(parts) > 1 else ctx.room.name

    full_transcript = "\n".join(chat_history)
    print(f"📡 CALL ENDED. Uploading to Airtable for: {caller_number}")

    try:
        # Use a timeout so the worker doesn't hang if Airtable is slow
        await asyncio.wait_for(
            log_to_airtable(caller_number, full_transcript, duration),
            timeout=15.0
        )
        print("✅ Airtable Upload Success")
    except Exception as e:
        print(f"❌ Airtable Error: {e}")


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    chat_history = []
    start_time = time.time()

    session = AgentSession(
        stt=deepgram.STT(model="nova-2-general", language="en-IN"),
        llm=groq.LLM(model="llama-3.1-8b-instant"),
        tts=elevenlabs.TTS(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id="pFZP5JQG7iQjIQuC4Bku",
            model="eleven_turbo_v2_5",
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.35,  # VERY LOW = More human emotion and natural flow
                similarity_boost=0.8,  # Keeps her sounding Indian
                style=0.5,  # Adds "air" and warmth
                use_speaker_boost=True
            )
        ),
        vad=silero.VAD.load(),
    )

    agent_instance = Agent(
        instructions=SYSTEM_PROMPT,
        tools=[get_room_info, get_hostel_policies]
    )

    @session.on("conversation_item_added")
    def on_conversation_item(event):
        item = event.item
        if item and item.content:
            # Check if content is a list and join it, or just use it if it's a string
            if isinstance(item.content, list):
                text = " ".join([str(c) for c in item.content]).strip()
            else:
                text = item.content.strip()

            if text:
                role_name = "Ankita" if item.role == "assistant" else "User"
                entry = f"{role_name}: {text}"
                print(f"✍️ TRAPPED: {entry}")
                chat_history.append(entry)

    await session.start(room=ctx.room, agent=agent_instance)

    await session.say(
        "Hello... -- and welcome to Princess Cottage. -- My name is Ankita. "
        "How can I help you with your inquiry today?",
        allow_interruptions=False
    )

    upload_done = False
    async def final_upload():
        nonlocal upload_done
        if not upload_done:
            upload_done = True
            await asyncio.shield(safe_upload(ctx, chat_history, start_time))

    ctx.add_shutdown_callback(final_upload)

    try:
        # Keep the agent alive as long as the participant is connected
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)

    except Exception as e:
        print(f"⚠️ Session Error: {e}")

    finally:
        await final_upload()
        print(f"🏁 Job Finished for {ctx.room.name}")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))