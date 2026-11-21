from collections import deque
import re
import subprocess
import time
import numpy as np
import requests
import sounddevice as sd
from faster_whisper import WhisperModel

# --- CONFIGURATION ---
N8N_VOICE_WEBHOOK = "http://localhost:5678/webhook/kaizen-voice"
PREFERRED_VOICES = ["Karen", "Daniel"]  # Uses the first voice installed on your Mac
SPEECH_RATE = "205"            # Brisk, conversational reply speed
SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.15           # 150ms frames for instant speech detection
SILENCE_DURATION = 0.55        # Stops recording 0.55s after you finish speaking
MAX_RECORD_SECONDS = 12

INITIAL_PROMPT = (
    "Kaizen. Hey Kaizen, open YouTube, Google Chrome, Visual Studio Code, "
    "Spotify, Notes, Calculator, check battery status, set volume, run shortcut."
)

WAKE_PATTERN = re.compile(
    r"\b(?:kaizen|kaisen|kai zen|kyzen|kyson|kaizan|kizen|kaiser|kaizin)\b[,:\s]*",
    re.IGNORECASE,
)


def pick_voice() -> str:
    out = subprocess.check_output(["say", "-v", "?"], text=True)
    installed = {line.split()[0] for line in out.splitlines() if line.strip()}
    for v in PREFERRED_VOICES:
        if v in installed:
            return v
    return "Daniel"


def pick_macbook_mic() -> int | None:
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and "MacBook" in dev["name"]:
            return idx
    return None


VOICE_NAME = pick_voice()
MIC_DEVICE = pick_macbook_mic()
http = requests.Session()

print("Loading fast local Whisper model (tiny.en)...")
whisper = WhisperModel("tiny.en", device="cpu", compute_type="int8")


def chime():
    """Play an instant non-blocking confirmation tick when wake word is heard."""
    subprocess.Popen(
        ["afplay", "/System/Library/Sounds/Tink.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def speak(text: str):
    clean = re.sub(r"[#*`~_>\[\]]", "", text)
    clean = clean.encode("ascii", "ignore").decode("ascii").strip() or "Done."
    print(f"KAIZEN ({VOICE_NAME}): {text}")
    subprocess.run(["say", "-v", VOICE_NAME, "-r", SPEECH_RATE, clean])


def init_noise_floor() -> deque:
    block_size = int(SAMPLE_RATE * BLOCK_SECONDS)
    history = deque(maxlen=30)
    with sd.InputStream(device=MIC_DEVICE, samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        for _ in range(4):  # 0.6s fast startup sample
            block, _ = stream.read(block_size)
            rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
            history.append(min(rms, 0.012))
    return history


def listen_utterance(ambient_history: deque, timeout_seconds: float | None = None) -> np.ndarray | None:
    block_size = int(SAMPLE_RATE * BLOCK_SECONDS)
    silent_blocks_needed = max(2, round(SILENCE_DURATION / BLOCK_SECONDS))
    max_blocks = int(MAX_RECORD_SECONDS / BLOCK_SECONDS)

    pre_buffer = deque(maxlen=4)  # Keeps 0.60s BEFORE trigger so "Kai-" is never clipped
    audio_chunks = []
    speaking = False
    silent_blocks = 0
    start_wait = time.time()

    with sd.InputStream(device=MIC_DEVICE, samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        while True:
            if not speaking and timeout_seconds and (time.time() - start_wait > timeout_seconds):
                return None

            block, _ = stream.read(block_size)
            mono = block[:, 0]
            rms = float(np.sqrt(np.mean(mono ** 2)))

            ambient = float(np.median(ambient_history)) if ambient_history else 0.003
            threshold = max(ambient * 2.0, 0.0052)

            if rms > threshold:
                if not speaking:
                    speaking = True
                    audio_chunks.extend(pre_buffer)
                silent_blocks = 0
                audio_chunks.append(mono.copy())
            elif speaking:
                audio_chunks.append(mono.copy())
                silent_blocks += 1
                if silent_blocks >= silent_blocks_needed or len(audio_chunks) >= max_blocks:
                    break
            else:
                pre_buffer.append(mono.copy())
                if rms < threshold * 1.15:
                    ambient_history.append(rms)

    return np.concatenate(audio_chunks) if audio_chunks else None


def transcribe(audio: np.ndarray) -> str:
    peak = float(np.max(np.abs(audio)))
    if peak > 0:
        gain = min(0.85 / peak, 10.0)
        audio = audio * gain
    segments, _ = whisper.transcribe(
        audio,
        beam_size=1,
        best_of=1,
        temperature=0.0,
        language="en",
        initial_prompt=INITIAL_PROMPT,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def send_to_kaizen(command: str) -> str:
    try:
        res = http.post(
            N8N_VOICE_WEBHOOK,
            json={"command": command, "sessionId": f"voice-{int(time.time() * 1000)}"},
            timeout=90,
        )
        res.raise_for_status()
        data = res.json()
        if isinstance(data, list) and data:
            data = data[0]
        return data.get("text") or data.get("output") or data.get("reply") or "Done."
    except Exception as e:
        return f"Could not reach KAIZEN in n8n: {e}"


def main():
    ambient_history = init_noise_floor()
    speak("Kaizen online.")
    print("Listening... Speak normally and say 'Kaizen' followed by your command.\n")

    while True:
        audio = listen_utterance(ambient_history)
        if audio is None or len(audio) < SAMPLE_RATE * 0.45:
            continue

        heard = transcribe(audio)
        if not heard:
            continue

        print(f"[Heard]: {heard}")
        match = WAKE_PATTERN.search(heard)
        if not match:
            continue

        chime()
        command = heard[match.end():].strip()

        if len(command) < 2:
            speak("Yes?")
            followup = listen_utterance(ambient_history, timeout_seconds=5.0)
            if followup is None:
                continue
            command = transcribe(followup)
            print(f"[Heard]: {command}")

        if command:
            reply = send_to_kaizen(command)
            speak(reply)
            time.sleep(0.25)


if __name__ == "__main__":
    main()
