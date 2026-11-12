"""The web page in a real browser, against the real app with a fake Whisper model."""
import threading
import time

import httpx
import pytest

from tests.fakes import FakeWhisper
from tests.helpers import RecordingWhisper

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # the browser is not installed: python -m playwright install chromium
            pytest.skip(f"no browser for the page tests: {e}")
        yield browser
        browser.close()


@pytest.fixture
def open_page(browser, live_server):
    """Start the app and open its page. Returns (page, base url); page.problems lists script errors."""
    contexts = []

    def _open(**app_options):
        base = live_server(**app_options)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        contexts.append(context)
        page = context.new_page()
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        page.goto(base)
        expect(page.locator("#models .model").first).to_be_visible()
        return page, base

    yield _open
    for context in contexts:
        context.close()


def add(page, *paths):
    page.set_input_files("#file-input", [str(p) for p in paths])


def create(page):
    expect(page.locator("#create")).to_be_enabled()
    page.locator("#create").click()


def make_subtitles(page, path):
    """One file from drop to the open editor."""
    add(page, path)
    create(page)
    expect(page.locator("#editor")).to_be_visible()
    expect(page.locator(".cue")).to_have_count(len(FakeWhisper.segments))


def job_id_of(page):
    return page.evaluate("GS.state.rows[0].jobId")


def saved(page):
    expect(page.locator("#save-state")).to_have_text("Saved")


def srt(base, job_id):
    return httpx.get(f"{base}/jobs/{job_id}/subtitles").text


# ---------- first look ----------

def test_empty_page_invites_a_file_and_cannot_start(open_page):
    page, _ = open_page()
    expect(page.locator("#caption")).to_contain_text("Drop a video or audio file here")
    expect(page.locator("#create")).to_be_disabled()
    expect(page.locator("#models .model")).to_have_count(4)
    expect(page.locator("#close-app")).to_be_hidden()
    assert page.problems == []


def test_time_helpers(open_page):
    page, _ = open_page()
    assert page.evaluate("GS.fmtTime(3725.0406)") == "1:02:05.041"
    assert page.evaluate("GS.fmtTime(4.38)") == "0:04.380"
    assert page.evaluate("GS.parseTime('1:02:05.041')") == pytest.approx(3725.041)
    assert page.evaluate("GS.parseTime('2:03,5')") == pytest.approx(123.5)
    assert page.evaluate("GS.parseTime('63.5')") == pytest.approx(63.5)
    assert page.evaluate("['abc', '1:2:3:4', '1.5:20', ''].map(GS.parseTime)") == [None, None, None, None]


def test_wrap_helper_balances_two_lines(open_page):
    page, _ = open_page()
    text = "This sentence is clearly longer than forty-two characters in total."
    lines = page.evaluate("t => GS.wrapTwo(t, 42)", text).split("\n")
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)
    assert page.evaluate("GS.wrapTwo('Short one.', 42)") == "Short one."


# ---------- from file to subtitles ----------

def test_one_file_goes_from_drop_to_the_open_editor(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    expect(page.locator(".row-meta")).to_contain_text("Detected: English (97%)")
    expect(page.locator(".row-meta")).to_contain_text("Fast · CPU")
    expect(page.locator(".cue textarea").first).to_have_value("Hello there.")
    expect(page.locator("#settings")).to_be_hidden()
    assert page.problems == []


def test_chosen_language_and_options_reach_the_model(open_page, media):
    page, _ = open_page()
    add(page, media["one"])
    page.locator("#language").select_option("ja")
    page.locator("input[name=task][value=translate]").check(force=True)
    page.locator("input[name=model][value=best]").check(force=True)
    create(page)
    expect(page.locator(".row-meta")).to_contain_text("Japanese")
    call = FakeWhisper.calls[0]
    assert (call["language"], call["task"], call["model_id"]) == ("ja", "translate", "large-v2")


def test_settings_are_remembered_after_a_reload(open_page):
    page, _ = open_page()
    page.locator("#language").select_option("tr")
    page.locator("input[name=style][value=vertical]").check(force=True)
    page.reload()
    expect(page.locator("#language")).to_have_value("tr")
    expect(page.locator("input[name=style][value=vertical]")).to_be_checked()


def test_turbo_with_translation_says_that_best_is_used(open_page):
    page, _ = open_page()
    expect(page.locator("#turbo-note")).to_be_hidden()
    page.locator("input[name=model][value=turbo]").check(force=True)
    page.locator("input[name=task][value=translate]").check(force=True)
    expect(page.locator("#turbo-note")).to_be_visible()


def test_file_with_two_audio_tracks_lets_the_user_pick_one(open_page, media):
    page, _ = open_page()
    add(page, media["two"])
    picker = page.locator(".row-track select")
    expect(picker).to_be_visible()
    expect(picker.locator("option")).to_have_count(2)
    expect(picker.locator("option").nth(1)).to_contain_text("rus")
    picker.select_option("1")
    create(page)
    expect(page.locator(".row.is-done")).to_have_count(1)
    assert FakeWhisper.calls[0]["wav_seconds"] == pytest.approx(3.0, abs=0.15)


def test_file_with_one_audio_track_shows_no_picker(open_page, media):
    page, _ = open_page()
    add(page, media["one"])
    expect(page.locator(".row-meta")).to_contain_text("ready")
    expect(page.locator(".row-track")).to_be_hidden()


def test_unreadable_file_shows_its_error_and_cannot_start(open_page, media):
    page, _ = open_page()
    add(page, media["broken"])
    expect(page.locator(".row.is-error .row-meta")).to_contain_text("could not be read")
    expect(page.locator("#create")).to_be_disabled()


def test_failed_job_shows_the_reason(open_page, media):
    page, _ = open_page()
    FakeWhisper.fail_on_device = {"cpu"}
    add(page, media["one"])
    create(page)
    expect(page.locator(".row.is-error .row-meta")).to_contain_text("cudnn")


def test_two_files_give_two_results_and_a_zip(open_page, media):
    page, _ = open_page()
    add(page, media["one"], media["two"])
    create(page)
    expect(page.locator(".row.is-done")).to_have_count(2)
    expect(page.locator("#zip")).to_be_visible()
    expect(page.locator("#editor")).to_be_hidden()      # with several files the editor waits for a choice
    with page.expect_download() as download:
        page.locator("#zip").click()
    assert download.value.suggested_filename == "subtitles.zip"


def test_download_is_named_after_the_file(open_page, media):
    page, _ = open_page()
    add(page, media["one"])
    create(page)
    expect(page.locator(".row.is-done")).to_have_count(1)
    with page.expect_download() as download:
        page.locator(".row .btn-small", has_text=".vtt").click()
    assert download.value.suggested_filename == "one track.vtt"


def test_a_waiting_file_can_be_removed_and_never_runs(open_page, media):
    page, base = open_page()
    RecordingWhisper.gate = threading.Event()
    add(page, media["one"], media["two"])
    create(page)
    expect(page.locator(".row").nth(1).locator(".row-meta")).to_contain_text("waiting in line (1 ahead)")
    page.locator(".row").nth(1).locator(".btn-x").click()
    expect(page.locator(".row")).to_have_count(1)
    RecordingWhisper.gate.set()
    expect(page.locator(".row.is-done")).to_have_count(1)
    time.sleep(0.3)
    assert len(FakeWhisper.calls) == 1
    assert httpx.get(f"{base}/health").json()["active_jobs"] == 0


def test_screen_caption_follows_the_job(open_page, media):
    page, _ = open_page()
    RecordingWhisper.gate = threading.Event()
    add(page, media["one"])
    expect(page.locator("#caption")).to_contain_text("Ready.")
    create(page)
    expect(page.locator("#caption")).to_contain_text("Listening")
    expect(page.locator("#caption")).not_to_contain_text("%")      # no 0% before the first part is done
    expect(page.locator(".row-meta")).to_contain_text("listening")
    RecordingWhisper.gate.set()
    expect(page.locator("#editor")).to_be_visible()


def test_result_survives_a_page_refresh(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    page.reload()
    expect(page.locator(".row.is-done")).to_have_count(1)
    expect(page.locator(".row-name")).to_have_text("one track.wav")
    page.locator(".row .btn", has_text="Edit").click()
    expect(page.locator(".cue")).to_have_count(2)
    expect(page.locator("#transport")).to_be_visible()      # the file plays from the app after a reload
    assert page.problems == []


def test_creating_again_asks_before_replacing_edits(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    page.locator(".cue textarea").first.fill("Changed.")
    saved(page)
    page.locator("#editor-back").click()
    expect(page.locator("#create")).to_have_text("Create again with these settings")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#create").click()
    time.sleep(0.3)
    assert len(FakeWhisper.calls) == 1


# ---------- editor ----------

def test_edited_text_is_saved_and_downloaded(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    page.locator(".cue textarea").first.fill("Hello, Kaan.")
    saved(page)
    assert "Hello, Kaan." in srt(base, job_id_of(page))
    expect(page.locator(".row-meta")).to_contain_text("edited")


def test_split_at_the_cursor_and_join_again(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    area = page.locator(".cue textarea").nth(1)          # "This is a test." from 1.0 to 2.0
    area.focus()
    area.evaluate("a => a.setSelectionRange(7, 7)")
    page.locator(".cue").nth(1).locator(".cue-tools button", has_text="Split").click()
    expect(page.locator(".cue")).to_have_count(3)
    expect(page.locator(".cue textarea").nth(1)).to_have_value("This is")
    expect(page.locator(".cue textarea").nth(2)).to_have_value("a test.")
    saved(page)
    cues = httpx.get(f"{base}/jobs/{job_id_of(page)}/cues").json()
    assert cues[1]["end"] == cues[2]["start"] and 1.0 < cues[1]["end"] < 2.0

    page.locator(".cue").nth(1).hover()
    page.locator(".cue").nth(1).locator(".cue-tools button", has_text="Join").click()
    expect(page.locator(".cue")).to_have_count(2)
    expect(page.locator(".cue textarea").nth(1)).to_have_value("This is a test.")


def test_delete_removes_a_line(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    page.locator(".cue").first.hover()
    page.locator(".cue").first.locator(".cue-tools button", has_text="Delete").click()
    expect(page.locator(".cue")).to_have_count(1)
    saved(page)
    assert "Hello there." not in srt(base, job_id_of(page))


def test_a_wrong_time_is_marked_and_not_saved(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    end = page.locator(".cue").first.locator(".cue-times input").nth(1)
    end.fill("nonsense")
    end.press("Enter")
    expect(end).to_have_class("is-bad")
    end.fill("0:00.000")          # would end before it starts
    end.press("Enter")
    expect(end).to_have_class("is-bad")
    end.fill("0:00.750")
    end.press("Enter")
    expect(end).not_to_have_class("is-bad")
    saved(page)
    assert "00:00:00,000 --> 00:00:00,750" in srt(base, job_id_of(page))


def test_find_and_replace_changes_every_line(open_page, media):
    page, base = open_page()
    FakeWhisper.segments = [("Kan said hello.", 0.0, 1.0), ("Then Kan left.", 1.0, 2.0)]
    make_subtitles(page, media["one"])
    page.locator(".tools summary").click()
    page.locator("#find").fill("Kan")
    page.locator("#replace").fill("Kaan")
    page.locator("#replace-all").click()
    expect(page.locator("#tool-note")).to_have_text("Replaced 2.")
    saved(page)
    assert srt(base, job_id_of(page)).count("Kaan") == 2


def test_moving_every_line_shifts_the_times(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    page.locator(".tools summary").click()
    page.locator("#shift").fill("1.5")
    page.locator("#shift-apply").click()
    saved(page)
    assert srt(base, job_id_of(page)).startswith("1\n00:00:01,500 --> 00:00:02,500\n")


def test_undo_all_edits_brings_the_first_result_back(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    page.locator(".cue textarea").first.fill("Changed.")
    saved(page)
    page.locator(".tools summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#reset-edits").click()
    expect(page.locator(".cue textarea").first).to_have_value("Hello there.")
    assert "Hello there." in srt(base, job_id_of(page))
    expect(page.locator(".row-meta")).not_to_contain_text("edited")


def test_a_line_that_is_too_long_is_marked(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    count = page.locator(".cue").first.locator(".cue-count")
    expect(count).to_have_text("12/42")
    expect(count).not_to_have_class("cue-count is-over")
    page.locator(".cue textarea").first.fill("x" * 50)
    expect(count).to_have_text("50/42")
    expect(count).to_have_class("cue-count is-over")


def test_the_screen_shows_the_line_at_the_playback_position(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    expect(page.locator("#transport")).to_be_visible()
    page.evaluate("document.getElementById('player').currentTime = 1.4")
    expect(page.locator("#caption")).to_have_text("This is a test.")
    expect(page.locator(".cue.is-now textarea")).to_have_value("This is a test.")


def test_the_screen_shows_the_line_being_typed(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    area = page.locator(".cue textarea").first
    area.fill("Typed just now.")
    expect(page.locator("#caption")).to_have_text("Typed just now.")


def test_a_file_the_browser_cannot_play_can_still_be_edited(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["unplayable"])
    expect(page.locator("#caption")).to_contain_text("cannot be played here")
    expect(page.locator("#transport")).to_be_hidden()
    page.locator(".cue textarea").first.fill("Still editable.")
    saved(page)
    assert "Still editable." in srt(base, job_id_of(page))


def test_back_to_settings_closes_the_editor(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    page.locator("#editor-back").click()
    expect(page.locator("#settings")).to_be_visible()
    expect(page.locator("#editor")).to_be_hidden()
    expect(page.locator("#caption")).to_contain_text("Done.")


# ---------- the app around the page ----------

def test_open_page_counts_as_an_open_tab(open_page):
    page, base = open_page()
    deadline = time.time() + 5
    while httpx.get(f"{base}/health").json()["open_tabs"] != 1:
        assert time.time() < deadline
        time.sleep(0.05)


def test_demo_shows_one_model_and_no_app_buttons(open_page):
    page, _ = open_page(demo=True)
    expect(page.locator("#models .model")).to_have_count(1)
    expect(page.locator("#demo")).to_be_visible()
    expect(page.locator("#demo-limit")).to_have_text("500")
    expect(page.locator("#privacy-demo")).to_be_visible()
    expect(page.locator("#clear-all")).to_be_hidden()
    expect(page.locator("#close-app")).to_be_hidden()


def test_demo_banner_says_what_the_demo_is_and_where_the_app_is(open_page):
    page, _ = open_page(demo=True)
    banner = page.locator("#demo")
    expect(banner).to_contain_text("shared processor")
    expect(banner).to_contain_text("500 MB and 30 minutes")
    expect(banner.locator("a", has_text="Download the app")).to_be_visible()
    expect(banner.locator("a", has_text="GitHub")).to_be_visible()
    assert httpx.get(f"{page.url}health").json()["open_tabs"] == 0     # no presence stream in the demo


def test_demo_refuses_a_too_large_file_before_sending_it(open_page, media, tmp_path):
    page, _ = open_page(demo=True, max_upload_bytes=1024)
    add(page, media["one"])
    expect(page.locator(".row.is-error .row-meta")).to_contain_text("The demo takes files up to")
    assert list((tmp_path / "data").glob("*.src")) == []
    expect(page.locator("#create")).to_be_disabled()


def test_demo_shows_the_place_in_line_behind_other_visitors(open_page, media):
    page, base = open_page(demo=True)
    RecordingWhisper.gate = threading.Event()
    with open(media["one"], "rb") as f:                         # another visitor, already running
        other = httpx.post(f"{base}/uploads", params={"filename": "other.wav"}, content=f.read()).json()
    httpx.post(f"{base}/jobs", json={"upload_id": other["upload_id"]})
    add(page, media["one"])
    create(page)
    expect(page.locator("#caption")).to_contain_text("Waiting in line.")
    expect(page.locator("#caption")).to_contain_text("1 file is ahead of yours.")
    RecordingWhisper.gate.set()
    expect(page.locator("#editor")).to_be_visible()


def test_the_app_page_still_says_starting_and_keeps_its_texts(open_page):
    page, base = open_page()
    expect(page.locator("#demo")).to_be_hidden()
    assert "Start GetSubtitles again" in page.locator("#offline").text_content()
    expect(page.locator("#device")).to_have_attribute("title", "The models run on the processor. "
                                                     "Fast and Balanced are the practical choices.")


def test_with_a_token_the_page_works_and_can_close_the_app(open_page, media):
    closed = []
    page, _ = open_page(token="secret", exit_fn=lambda: closed.append(1))
    make_subtitles(page, media["one"])
    page.locator(".cue textarea").first.fill("With a token.")
    saved(page)
    page.locator("#close-app").click()
    expect(page.locator(".closed")).to_contain_text("Closed. You can close this tab.")
    assert closed == [1]


def test_free_memory_removes_the_files(open_page, media):
    page, base = open_page()
    make_subtitles(page, media["one"])
    job_id = job_id_of(page)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#clear-all").click()
    expect(page.locator(".row")).to_have_count(0)
    expect(page.locator("#settings")).to_be_visible()
    assert httpx.get(f"{base}/jobs/{job_id}").status_code == 404


def test_page_says_when_the_app_is_gone(open_page, live_server):
    page, _ = open_page()
    expect(page.locator("#offline")).to_be_hidden()
    live_server.servers[-1].should_exit = True
    expect(page.locator("#offline")).to_be_visible(timeout=15000)


def test_a_full_length_line_stays_on_one_line_on_the_screen(open_page, media):
    page, _ = open_page()
    make_subtitles(page, media["one"])
    page.locator(".cue textarea").first.fill("ask not what your country can do for you. x")   # 42 characters
    caption = page.locator("#caption")
    expect(caption).to_have_text("ask not what your country can do for you. x")
    lines = caption.evaluate("n => Math.round(n.getBoundingClientRect().height / parseFloat(getComputedStyle(n).lineHeight))")
    assert lines == 1


def test_first_run_shows_the_model_download(open_page, media):
    gate = threading.Event()

    def download(size, on_progress):
        on_progress(0.4)
        gate.wait(timeout=10)
        return size

    page, _ = open_page(download=download)
    add(page, media["one"])
    create(page)
    expect(page.locator(".row-meta")).to_contain_text("downloading the model: 40%")
    expect(page.locator("#caption")).to_contain_text("Downloading the model")
    gate.set()
    expect(page.locator("#editor")).to_be_visible()


def test_on_a_phone_wide_screen_nothing_is_squeezed_or_cut_off(open_page, media):
    page, _ = open_page()
    page.set_viewport_size({"width": 390, "height": 844})
    make_subtitles(page, media["one"])
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
    assert page.locator(".row-name").bounding_box()["width"] > 250      # the name has the full row
    assert page.locator(".row-meta").bounding_box()["height"] < 50      # the status is not a column of words
    expect(page.locator(".cue-count").first).to_be_in_viewport()
    box = page.locator(".cue-count").first.bounding_box()
    assert box["x"] + box["width"] <= 390


def test_footer_shows_where_models_are_kept_and_opens_the_folder(open_page, tmp_path):
    opened = []
    storage = tmp_path / "GetSubtitles"
    snapshot = storage / "hf" / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x" * 3 * 1024 * 1024)
    page, _ = open_page(token="secret", storage_dir=storage, models_cache=storage / "hf" / "hub",
                        open_folder=opened.append)
    expect(page.locator("#storage")).to_be_visible()
    expect(page.locator("#storage")).to_contain_text(str(storage))
    expect(page.locator("#storage")).to_contain_text("Fast 3.0 MB")
    page.locator("#open-storage").click()
    for _ in range(50):
        if opened:
            break
        time.sleep(0.05)
    assert opened == [storage]
    assert page.problems == []


def test_footer_hides_storage_without_a_folder(open_page):
    page, _ = open_page()
    expect(page.locator("#storage")).to_be_hidden()


def test_each_model_choice_names_its_whisper_model(open_page):
    page, _ = open_page()
    expect(page.locator(".model").first).to_contain_text("Whisper small")
    expect(page.locator("#models")).to_contain_text("Whisper large-v2")


def test_gpu_offer_downloads_gpu_support(open_page, tmp_path):
    done = []

    def fake_install(folder, on_progress):
        on_progress(1.0)
        done.append(folder)

    page, _ = open_page(token="secret", storage_dir=tmp_path, gpu_install=fake_install, gpu_present=lambda: True)
    expect(page.locator("#gpu-offer")).to_be_visible()
    expect(page.locator("#gpu-offer")).to_contain_text("NVIDIA")
    page.locator("#gpu-install").click()
    for _ in range(50):
        if done:
            break
        time.sleep(0.05)
    assert done == [tmp_path / "cuda"]
    assert page.problems == []


def test_gpu_offer_shows_the_download_going_up(open_page, tmp_path):
    step = threading.Event()

    def slow_install(folder, on_progress):
        folder.mkdir(parents=True)
        for fraction in (0.1, 0.5, 0.995):
            on_progress(fraction)
            step.wait(5)
            step.clear()

    page, _ = open_page(token="secret", storage_dir=tmp_path, gpu_install=slow_install, gpu_present=lambda: True)
    page.locator("#gpu-install").click()
    expect(page.locator("#gpu-offer")).to_contain_text("10%")
    step.set()
    expect(page.locator("#gpu-offer")).to_contain_text("50%")
    step.set()
    expect(page.locator("#gpu-offer")).to_contain_text("unpacking")
    step.set()
    # the fake engine stays on the CPU, so the page says the GPU could not be used
    expect(page.locator("#gpu-offer")).to_contain_text("could not be used")
    expect(page.locator("#gpu-install")).to_be_hidden()
    assert page.problems == []


def test_gpu_offer_is_hidden_without_a_gpu(open_page, tmp_path):
    page, _ = open_page(token="secret", storage_dir=tmp_path, gpu_install=lambda f, p: None,
                        gpu_present=lambda: False)
    expect(page.locator("#gpu-offer")).to_be_hidden()
