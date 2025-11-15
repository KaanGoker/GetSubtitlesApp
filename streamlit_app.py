import os, sys
import time
from pathlib import Path
import threading
import requests
import streamlit as st
from PIL import Image

def resource_path(rel_path: str) -> str:
    """Resolve path for dev and PyInstaller onefile (_MEIPASS)"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    return os.path.join(base, rel_path)

st.set_page_config(
    page_title="Get Subtitles",
    page_icon=Image.open(resource_path("icon assets/getsubtitles.png")),
    layout="centered",
)
HIDE_UI = """
<style>
/* Hide Streamlit toolbar + hamburger + footer */
div[data-testid="stToolbar"] { display: none !important; }
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header { visibility: hidden; }  /* hides the top header line */
</style>
"""
st.markdown(HIDE_UI, unsafe_allow_html=True)
st.title("Get Subtitles in SRT Format")
st.markdown("""
* The system will automatically detect the language of the uploaded video/audio.
* Once the file is loaded, please wait for the **"Create subtitles"** button to appear.
* If you have a large video, uploading only the audio is much faster.
* It works better with files without background music.
""")

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"

BACKEND = os.environ.get("BACKEND_URL", DEFAULT_BACKEND_URL)

MODEL_LABELS = [
    "**Fast** — Shorter time, lower accuracy.",
    "**Balanced** — Longer time, better accuracy.",
    "**Best** — Highest accuracy; slowest and needs a strong GPU.",
]
MODEL_MAP = {
    MODEL_LABELS[0]: "fast",
    MODEL_LABELS[1]: "balanced",
    MODEL_LABELS[2]: "best",
}
MODEL_DISPLAY = {"fast": "Fast", "balanced": "Balanced", "best": "Best"}

LANGUAGE_NAMES = {
    "af":"Afrikaans","am":"Amharic","ar":"Arabic","as":"Assamese","az":"Azerbaijani","ba":"Bashkir",
    "be":"Belarusian","bg":"Bulgarian","bn":"Bengali","bo":"Tibetan","br":"Breton","bs":"Bosnian",
    "ca":"Catalan","cs":"Czech","cy":"Welsh","da":"Danish","de":"German","el":"Greek","en":"English",
    "es":"Spanish","et":"Estonian","eu":"Basque","fa":"Persian","fi":"Finnish","fo":"Faroese",
    "fr":"French","gl":"Galician","gu":"Gujarati","ha":"Hausa","haw":"Hawaiian","he":"Hebrew",
    "hi":"Hindi","hr":"Croatian","ht":"Haitian","hu":"Hungarian","hy":"Armenian","id":"Indonesian",
    "is":"Icelandic","it":"Italian","ja":"Japanese","jw":"Javanese","ka":"Georgian","kk":"Kazakh",
    "km":"Khmer","kn":"Kannada","ko":"Korean","la":"Latin","lb":"Luxembourgish","ln":"Lingala",
    "lo":"Lao","lt":"Lithuanian","lv":"Latvian","mg":"Malagasy","mi":"Maori","mk":"Macedonian",
    "ml":"Malayalam","mn":"Mongolian","mr":"Marathi","ms":"Malay","mt":"Maltese","my":"Burmese",
    "ne":"Nepali","nl":"Dutch","no":"Norwegian","oc":"Occitan","pa":"Punjabi","pl":"Polish","ps":"Pashto",
    "pt":"Portuguese","ro":"Romanian","ru":"Russian","sa":"Sanskrit","sd":"Sindhi","si":"Sinhala",
    "sk":"Slovak","sl":"Slovenian","sn":"Shona","so":"Somali","sq":"Albanian","sr":"Serbian",
    "su":"Sundanese","sv":"Swedish","sw":"Swahili","ta":"Tamil","te":"Telugu","tg":"Tajik","th":"Thai",
    "tk":"Turkmen","tl":"Tagalog","tr":"Turkish","tt":"Tatar","uk":"Ukrainian","ur":"Urdu","uz":"Uzbek",
    "vi":"Vietnamese","yi":"Yiddish","yo":"Yoruba","zh":"Chinese"
}

def pretty_lang(code: str) -> str:
    return LANGUAGE_NAMES.get((code or "").lower(), (code or "Unknown").upper())

def fmt_mmss(seconds):
    if seconds is None:
        return "?:??"
    seconds = max(0, int(round(float(seconds))))
    return f"{seconds // 60}:{seconds % 60:02d}"

selected_label = st.radio("Model", MODEL_LABELS, index=0)
model_choice = MODEL_MAP[selected_label]

SUB_LABELS = ["Same as audio/video", "English (translate)"]
task_value = "transcribe" if st.radio("Subtitle language", SUB_LABELS, index=0) == SUB_LABELS[0] else "translate"

STYLE_LABELS = ["Default Video (16:9 / Longer lines)", "Vertical Video (9:16 / Shorter Subtitles for Reels/Shorts)"]
STYLE_MAP = {
    STYLE_LABELS[0]: "default",
    STYLE_LABELS[1]: "vertical",
}
style_choice = STYLE_MAP[st.radio("Subtitle style", STYLE_LABELS, index=0,
                                  help="Use Vertical for reels/shorts: shorter, faster-changing single-line captions.")]

file = st.file_uploader("Upload audio/video", type=None)

# Run job
if file and st.button("Create subtitles"):
    try:
        start_resp = requests.post(
            f"{BACKEND}/transcribe_start",
            files={"file": (file.name, file.getvalue())},
            data={
                "language": "auto",
                "task": task_value,
                "model_choice": model_choice,
                "style": style_choice,  # << NEW
            },
            timeout=None,
        )
        start_resp.raise_for_status()
        job = start_resp.json()
        job_id = job["job_id"]

        prog = st.progress(0, text="Starting…")
        status_text = st.empty()
        t0 = time.time()
        t_running_start = None
        
        while True:
            pr = requests.get(f"{BACKEND}/progress/{job_id}", timeout=30)
            pr.raise_for_status()
            info = pr.json()

            if info["status"] == "loading_model":
                prog.progress(0, text="Please wait..")
                status_text.info(
                    "The selected AI model is being downloaded/loaded. "
                    "This may take a while on the first run."
                )

            elif info["status"] == "running":
                if t_running_start is None:
                    t_running_start = time.time()

                real_pct = int(round((info.get("progress") or 0.0) * 100))

                elapsed_running = time.time() - t_running_start
                SIMULATED_DURATION_SECONDS = 15.0
                SIMULATED_PROGRESS_CAP = 15
                
                simulated_pct = (elapsed_running / SIMULATED_DURATION_SECONDS) * SIMULATED_PROGRESS_CAP
                simulated_pct = min(int(simulated_pct), SIMULATED_PROGRESS_CAP)
                
                display_pct = max(real_pct, simulated_pct)

                prog.progress(min(max(display_pct, 0), 100), text=f"{display_pct}%")
                
                eta = info.get("eta_sec")
                model_pretty = MODEL_DISPLAY.get(info.get("model_choice",""), info.get("model_name",""))
                lang_pretty = pretty_lang(info.get("language"))
                
                status_text.info(f"Processing… {display_pct}% • ETA: {fmt_mmss(eta)} • Model: {model_pretty}")
            
            elif info["status"] == "done":
                elapsed = time.time() - t0
                st.success(f"Done in {fmt_mmss(elapsed)} — Detected: {lang_pretty} • Model: {model_pretty}")

                suggested = Path(job.get("original_filename","subtitle")).with_suffix(".srt").name
                srt_url = info.get("srt_url")

                if srt_url and "disabled_for_custom_dir" not in srt_url:
                    sr = requests.get(f"{BACKEND}{srt_url}", timeout=None)
                    if sr.ok:
                        st.download_button(
                            "Save .srt",
                            sr.content,
                            file_name=suggested,
                            mime="text/plain",
                            use_container_width=True,
                        )
                    else:
                        st.error("Could not fetch SRT via API.")
                else:
                    p = info.get("srt_path")
                    if p and Path(p).exists():
                        st.download_button(
                            "Save .srt",
                            Path(p).read_bytes(),
                            file_name=suggested,
                            mime="text/plain",
                            use_container_width=True,
                        )
                    else:
                        st.info("SRT created. Open the outputs folder to find it.")
                break

            elif info["status"] == "error":
                st.error(f"Failed: {info.get('error','unknown error')}")
                break

            time.sleep(0.5)

    except Exception as e:
        st.error(f"Request failed: {e}")

st.divider()

if st.button("Clear cache", type="primary", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

if st.button("Close the app", type="primary", use_container_width=True):
    try:
        requests.post(f"{BACKEND}/shutdown", timeout=1.0)
    except Exception:
        pass  # server may already be down

    def _quit():
        import time
        time.sleep(0.2)
        os._exit(0)
    threading.Thread(target=_quit, daemon=True).start()

    st.info("Closing…")
    st.stop()

    st.markdown(
    "<p style='text-align: center; color: #666; font-size: 0.9rem;'>OpenAI's Whisper is used in this app</p>", 
    unsafe_allow_html=True
)