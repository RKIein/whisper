from datetime import datetime

import course_calendar as cc

# Same shape as a TraiNex "Studienplan" export (names are made up)
TRAINEX_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:M13 I Multivariate Verfahren, Forschungsmethoden & Psychotherapieforschung I/Seminar - R1.0 (Seminarraum)  -  t25_abc21
DESCRIPTION:M13 I Multivariate Verfahren, Forschungsmethoden & Psychotherapieforschung I/Seminar - R1.0 (Seminarraum) - Psychologie mit Schwerpunkt Klinische Psychologie (D_Master PT WS 2026-3)/Erika Mustermann  ab 11:30 Uhr - Aktuelle Termine immer im TraiNex prüfen.
DTSTART:20261005T113000
DTEND:20261005T130000
CATEGORIES:TraiNex
LOCATION:R1.0 (Seminarraum) - Campus // Musterstraße 1 // 12345 Stadt
END:VEVENT

BEGIN:VEVENT
SUMMARY:M4 Dokumentation und Qualitätssicherung/Vorlesung - ZH 006.1 Hörsaal  -  t25_abc21
DESCRIPTION:M4 Dokumentation und Qualitätssicherung/Vorlesung - ZH 006.1 Hörsaal - Psychologie mit Schwerpunkt Klinische Psychologie (D_Master PT WS 2026-3)/Prof. Dr. Max Beispiel  ab 08:00 Uhr - Aktuelle Termine immer im TraiNex prüfen.
DTSTART:20261006T080000
DTEND:20261006T093000
LOCATION:ZH 006.1 Hörsaal  - Campus // Musterstraße 1 // 12345 Stadt
END:VEVENT

BEGIN:VEVENT
SUMMARY:M8 Psychotherapeutische Basiskompetenzen/VT - R1.2 (Seminarraum)  -  t25_abc21
DESCRIPTION:M8 Psychotherapeutische Basiskompetenzen/VT - R1.2 (Seminarraum) - Grp. Master XY WS 26-VI  ab 12:30 Uhr - Aktuelle Termine immer im TraiNex prüfen. (Leiter/-in: Dr. Anna Probe)
DTSTART:20261006T123000
DTEND:20261006T140000
LOCATION:R1.2 (Seminarraum) - Campus // Musterstraße 1 // 12345 Stadt
END:VEVENT

BEGIN:VEVENT
SUMMARY:M5 Diagnostische Modelle & Methoden/Seminar  -  t25_abc21
DESCRIPTION:M5 Diagnostische Modelle & Methoden/Seminar - Psychologie mit Schwerpunkt Klinische Psychologie (D_Master PT WS 2026-3)/Prof. Dr. Max Beispiel  ab 15:30 Uhr - Aktuelle Termine immer im TraiNex prüfen.
DTSTART:20261006T153000
DTEND:20261006T170000
LOCATION:  - Campus // Musterstraße 1 // 12345 Stadt
END:VEVENT
END:VCALENDAR
"""


def test_parse_trainex_fields():
    events = cc.parse_ics(TRAINEX_ICS)
    assert len(events) == 4
    seminar = events[0]
    assert seminar.course == "M13 I Multivariate Verfahren, Forschungsmethoden & Psychotherapieforschung I"
    assert seminar.kind == "Seminar"
    assert seminar.room == "R1.0 (Seminarraum)"
    assert seminar.lecturer == "Erika Mustermann"
    assert seminar.program == "Psychologie mit Schwerpunkt Klinische Psychologie (D_Master PT WS 2026-3)"
    assert seminar.short_label == "M13 Seminar · R1.0"

    lecture = events[1]
    assert (lecture.kind, lecture.room, lecture.lecturer) == ("Vorlesung", "ZH 006.1 Hörsaal", "Prof. Dr. Max Beispiel")

    vt = events[2]
    assert (vt.kind, vt.lecturer, vt.program) == ("VT", "Dr. Anna Probe", "Grp. Master XY WS 26-VI")

    no_room = events[3]
    assert no_room.kind == "Seminar" and no_room.room == ""


def test_find_current_event():
    events = cc.parse_ics(TRAINEX_ICS)
    # 10 minutes early still matches
    assert cc.find_current_event(events, datetime(2026, 10, 6, 7, 50)).kind == "Vorlesung"
    assert cc.find_current_event(events, datetime(2026, 10, 6, 9, 20)).kind == "Vorlesung"
    assert cc.find_current_event(events, datetime(2026, 10, 6, 10, 30)) is None
    assert cc.find_current_event(events, datetime(2026, 10, 6, 12, 20)).kind == "VT"


def test_generic_calendar_with_folding_rrule_and_utc():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:Analysis II\\, Vorlesung\r\n"
        "DTSTART;TZID=Europe/Berlin:20261005T100000\r\n"
        "DTEND;TZID=Europe/Berlin:20261005T113000\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "EXDATE;TZID=Europe/Berlin:20261012T100000\r\n"
        "LOCATION:Hörsaal 1 - Haupt\r\n gebäude\r\n"
        "BEGIN:VALARM\r\nSUMMARY:ignore me\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events = cc.parse_ics(ics)
    assert [e.start.day for e in events] == [5, 19]       # 12th excluded
    assert events[0].summary == "Analysis II, Vorlesung"
    assert events[0].course == "Analysis II, Vorlesung"
    assert events[0].location == "Hörsaal 1 - Hauptgebäude"
    assert events[0].end - events[0].start == (events[1].end - events[1].start)


def test_calendar_import_and_cache(tmp_path, monkeypatch):
    src = tmp_path / "plan.ics"
    src.write_text(TRAINEX_ICS, encoding="utf-8")
    target = tmp_path / "calendar.ics"
    monkeypatch.setattr(cc, "CALENDAR_FILE", str(target))
    assert cc.import_file(str(src)) == 4
    cal = cc.CourseCalendar(path=str(target))
    assert cal.available
    ev = cal.current(datetime(2026, 10, 5, 12, 0))
    assert ev.lecturer == "Erika Mustermann"
    assert ev.to_meta()["start"] == "2026-10-05T11:30"
