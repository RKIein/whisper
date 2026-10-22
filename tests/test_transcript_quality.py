import lecture
from transcriber import clean_segment, strip_prompt_echo


def test_prompt_echo_removed():
    prompt = "Seminar: M13 I Multivariate Verfahren, Forschungsmethoden & Psychotherapieforschung I."
    # real outputs from the seminar test
    assert strip_prompt_echo("M13 I. M13 I. M13 I. Das ist jetzt ein Ultra-Werk, ne?", prompt) \
        == "Das ist jetzt ein Ultra-Werk, ne?"
    assert strip_prompt_echo("M13 I Multivariate Verfahren & Psychotherapieforschung I. I. I.", prompt) == ""
    kept = "Dann öffnet sich ein Dropdown-Menü, in dem Sie das Skalenniveau verändern."
    assert strip_prompt_echo(kept, prompt) == kept
    assert strip_prompt_echo(kept, None) == kept


def test_repetition_loops_collapse():
    assert clean_segment("Ja. Das ist so. Das ist so. Das ist so. Das ist so.") == "Ja. Das ist so."
    assert clean_segment("Gerne. Gerne. Gerne. Bis morgen.") == "Gerne. Bis morgen."
    assert clean_segment("Ja, ja, ja, ja, ja, ja.") == "Ja."
    # two in a row can be real speech
    assert clean_segment("Nein, nein, so nicht.") == "Nein, nein, so nicht."


def test_silence_fillers_dropped():
    assert clean_segment("Thank you very much.") == ""
    assert clean_segment("... ... ...") == ""
    assert clean_segment("Thank you very much for the question, it shows") != ""


def test_language_lock_settles_on_first_clear_chunk():
    lock = lecture.LanguageLock("auto")
    assert lock.current == "auto"
    assert not lock.observe("ru", 1)        # one stray segment in a quiet minute
    assert lock.current == "auto"
    assert lock.observe("de", 12)
    assert not lock.observe("pl", 7)        # later noise can't switch it
    assert lock.current == "de"
    assert lecture.LanguageLock("en").current == "en"


def test_context_prompt():
    assert lecture.context_prompt({"course": "M13 Multivariate Verfahren", "kind": "Seminar"}) \
        == "Seminar: M13 Multivariate Verfahren."
    assert lecture.context_prompt({"course": "Statistik"}) == "Statistik."
    assert lecture.context_prompt({}) is None
